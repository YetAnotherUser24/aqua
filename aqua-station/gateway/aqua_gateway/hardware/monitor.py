"""Hardware monitoring driver for Raspberry Pi gateway HAT.

Reads:
* 2x INA226 power monitors on I2C bus 1:
  - U11 (0x40): Input power (Vin / 12V), 10 mΩ shunt.
  - U13 (0x41): 5V output power (+5V_out), 10 mΩ shunt.
* RTC DS3231MZ (0x68):
  - Temperature reading (sysfs hwmon with direct I2C fallback).
  - Oscillator status / validity check.
* Raspberry Pi CPU core temperature:
  - Read from /sys/class/thermal/thermal_zone0/temp.
"""

from __future__ import annotations

import glob
import logging
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from smbus2 import SMBus
except ImportError:
    SMBus = None  # type: ignore[assignment,misc]

log = logging.getLogger("aqua.gateway.hardware")


@dataclass(slots=True)
class PowerReading:
    """Readings from an INA226 current/voltage sensor."""

    voltage_v: float
    current_a: float
    power_w: float
    shunt_mv: float


@dataclass(slots=True)
class RtcReading:
    """Status and temperature from DS3231 RTC."""

    temp_c: float | None
    valid: bool
    status: str  # "ok", "power_lost", "unavailable"


@dataclass(slots=True)
class HardwareReading:
    """Combined hardware health and telemetry snapshot."""

    ts: float
    available: bool
    cpu_temp_c: float | None
    rtc: RtcReading
    power_in: PowerReading | None
    power_5v: PowerReading | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "available": self.available,
            "cpu_temp_c": self.cpu_temp_c,
            "rtc": asdict(self.rtc),
            "power_in": asdict(self.power_in) if self.power_in else None,
            "power_5v": asdict(self.power_5v) if self.power_5v else None,
        }


class INA226:
    """Driver for Texas Instruments INA226 I2C current and power monitor."""

    REG_CONFIG = 0x00
    REG_SHUNT_VOLTAGE = 0x01
    REG_BUS_VOLTAGE = 0x02
    REG_POWER = 0x03
    REG_CURRENT = 0x04
    REG_CALIBRATION = 0x05

    CONFIG_RESET = 0x8000
    CONFIG_AVG_16 = 0x0400
    CONFIG_VBUS_CT_1100US = 0x0100
    CONFIG_VSH_CT_1100US = 0x0020
    CONFIG_MODE_SHUNT_AND_BUS_CONTINUOUS = 0x0007

    def __init__(
        self,
        bus_num: int = 1,
        address: int = 0x40,
        shunt_ohms: float = 0.010,
        max_expected_amps: float = 8.0,
    ) -> None:
        self.bus_num = bus_num
        self.address = address
        self.shunt_ohms = shunt_ohms
        self.max_expected_amps = max_expected_amps
        self._configured = False

        self.current_lsb = max_expected_amps / 32768.0
        self.power_lsb = 25.0 * self.current_lsb
        self.calibration_value = int(0.00512 / (self.current_lsb * self.shunt_ohms))

    def _ensure_configured(self, bus: Any) -> None:
        if self._configured:
            return
        config = (
            self.CONFIG_AVG_16
            | self.CONFIG_VBUS_CT_1100US
            | self.CONFIG_VSH_CT_1100US
            | self.CONFIG_MODE_SHUNT_AND_BUS_CONTINUOUS
        )
        self._write_register(bus, self.REG_CONFIG, config)
        self._write_register(bus, self.REG_CALIBRATION, self.calibration_value)
        self._configured = True

    def _read_register(self, bus: Any, register: int) -> int:
        data = bus.read_i2c_block_data(self.address, register, 2)
        return (data[0] << 8) | data[1]

    def _write_register(self, bus: Any, register: int, value: int) -> None:
        data = [(value >> 8) & 0xFF, value & 0xFF]
        bus.write_i2c_block_data(self.address, register, data)

    @staticmethod
    def _to_signed(value: int) -> int:
        if value > 32767:
            return value - 65536
        return value

    def read(self, bus: Any) -> PowerReading:
        self._ensure_configured(bus)
        raw_bus = self._read_register(bus, self.REG_BUS_VOLTAGE)
        voltage_v = round(raw_bus * 0.00125, 3)

        raw_shunt = self._to_signed(self._read_register(bus, self.REG_SHUNT_VOLTAGE))
        shunt_mv = round(raw_shunt * 0.0025, 3)

        raw_curr = self._to_signed(self._read_register(bus, self.REG_CURRENT))
        current_a = round(raw_curr * self.current_lsb, 3)

        raw_pwr = self._read_register(bus, self.REG_POWER)
        power_w = round(raw_pwr * self.power_lsb, 3)

        return PowerReading(
            voltage_v=max(voltage_v, 0.0),
            current_a=current_a,
            power_w=power_w,
            shunt_mv=shunt_mv,
        )


class HardwareMonitor:
    """Coordinates hardware readings with TTL caching and fail-safe handling."""

    def __init__(
        self,
        bus_num: int = 1,
        vin_address: int = 0x40,
        vout_5v_address: int = 0x41,
        rtc_address: int = 0x68,
        cache_ttl_s: float = 2.5,
    ) -> None:
        self.bus_num = bus_num
        self.rtc_address = rtc_address
        self.cache_ttl_s = cache_ttl_s

        # Both U11 and U13 use 10 mΩ shunts as shown in the schematic
        self.sensor_vin = INA226(
            bus_num=bus_num,
            address=vin_address,
            shunt_ohms=0.010,
            max_expected_amps=8.0,
        )
        self.sensor_5v = INA226(
            bus_num=bus_num,
            address=vout_5v_address,
            shunt_ohms=0.010,
            max_expected_amps=8.0,
        )

        self._last_reading: HardwareReading | None = None
        self._last_read_time: float = 0.0

    def read_cpu_temp(self) -> float | None:
        """Read CPU temperature from Linux sysfs thermal zone, with vcgencmd fallback."""
        thermal_path = Path("/sys/class/thermal/thermal_zone0/temp")
        if thermal_path.is_file():
            try:
                raw = thermal_path.read_text().strip()
                if raw.isdigit():
                    return round(int(raw) / 1000.0, 1)
            except Exception as exc:
                log.debug("failed reading %s: %s", thermal_path, exc)

        # Fallback to vcgencmd
        try:
            res = subprocess.run(
                ["vcgencmd", "measure_temp"],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )
            if res.returncode == 0:
                match = re.search(r"temp=([\d\.]+)'C", res.stdout)
                if match:
                    return round(float(match.group(1)), 1)
        except Exception:
            pass

        return None

    def read_rtc_temp_sysfs(self) -> float | None:
        """Read DS3231 temperature exposed by kernel driver in sysfs."""
        patterns = [
            "/sys/class/rtc/rtc0/device/hwmon/hwmon*/temp1_input",
            "/sys/class/hwmon/hwmon*/temp1_input",
        ]
        for pattern in patterns:
            for path_str in glob.glob(pattern):
                path = Path(path_str)
                try:
                    # Verify it's ds3231 if name file exists
                    name_file = path.parent / "name"
                    if name_file.is_file():
                        name = name_file.read_text().strip()
                        if "ds3231" not in name and "rtc" not in name:
                            continue
                    val = path.read_text().strip()
                    if val.lstrip("-").isdigit():
                        return round(int(val) / 1000.0, 2)
                except Exception as exc:
                    log.debug("error reading rtc temp from %s: %s", path, exc)
        return None

    def read_rtc(self, bus: Any | None) -> RtcReading:
        """Read DS3231 temperature and status."""
        temp_c = self.read_rtc_temp_sysfs()
        valid = False
        status = "unavailable"

        # Check if sysfs rtc0 exists
        rtc0_dir = Path("/sys/class/rtc/rtc0")
        if rtc0_dir.is_dir():
            status = "ok"
            valid = True
            # Check since_epoch file if available
            since_epoch = rtc0_dir / "since_epoch"
            if since_epoch.is_file():
                try:
                    ts = int(since_epoch.read_text().strip())
                    # If RTC date is before 2024, oscillator had lost power
                    if ts < 1704067200:
                        status = "power_lost"
                        valid = False
                except Exception:
                    pass

        # If temperature wasn't found in sysfs and I2C bus is available, read registers 0x11/0x12
        if temp_c is None and bus is not None:
            try:
                # REG_TEMP_MSB = 0x11, REG_TEMP_LSB = 0x12, REG_STATUS = 0x0F
                msb = bus.read_byte_data(self.rtc_address, 0x11)
                lsb = bus.read_byte_data(self.rtc_address, 0x12)
                if msb & 0x80:
                    temp = (msb - 256) + ((lsb >> 6) * 0.25)
                else:
                    temp = msb + ((lsb >> 6) * 0.25)
                temp_c = round(temp, 2)

                # Check OSF (Oscillator Stop Flag) in Status Register (0x0F, bit 7)
                reg_status = bus.read_byte_data(self.rtc_address, 0x0F)
                if reg_status & 0x80:
                    status = "power_lost"
                    valid = False
                else:
                    status = "ok"
                    valid = True
            except Exception as exc:
                log.debug("direct I2C read from RTC at 0x%02X failed: %s", self.rtc_address, exc)

        return RtcReading(temp_c=temp_c, valid=valid, status=status)

    def read_all(self, *, force: bool = False) -> HardwareReading:
        """Read all hardware telemetry, returning cached reading if within TTL."""
        now = time.time()
        if not force and self._last_reading is not None and (now - self._last_read_time) < self.cache_ttl_s:
            return self._last_reading

        cpu_temp = self.read_cpu_temp()
        power_in: PowerReading | None = None
        power_5v: PowerReading | None = None
        bus = None

        if SMBus is not None:
            try:
                bus = SMBus(self.bus_num)
            except Exception as exc:
                log.debug("could not open I2C bus %d: %s", self.bus_num, exc)
                bus = None

        try:
            if bus is not None:
                try:
                    power_in = self.sensor_vin.read(bus)
                except Exception as exc:
                    log.debug("INA226 Vin (0x%02X) read failed: %s", self.sensor_vin.address, exc)

                try:
                    power_5v = self.sensor_5v.read(bus)
                except Exception as exc:
                    log.debug("INA226 5V (0x%02X) read failed: %s", self.sensor_5v.address, exc)

            rtc = self.read_rtc(bus)
        finally:
            if bus is not None:
                try:
                    bus.close()
                except Exception:
                    pass

        available = any(
            (
                cpu_temp is not None,
                power_in is not None,
                power_5v is not None,
                rtc.temp_c is not None or rtc.status == "ok",
            )
        )

        reading = HardwareReading(
            ts=now,
            available=available,
            cpu_temp_c=cpu_temp,
            rtc=rtc,
            power_in=power_in,
            power_5v=power_5v,
        )

        self._last_reading = reading
        self._last_read_time = now
        return reading

    def snapshot(self) -> dict[str, Any]:
        """Convenience method returning a JSON-serializable dictionary."""
        return self.read_all().as_dict()

