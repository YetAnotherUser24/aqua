"""Unit tests for the HAT HardwareMonitor, INA226 and DS3231 drivers."""

from unittest.mock import MagicMock, patch

from aqua_gateway.hardware.monitor import INA226, HardwareMonitor


def test_ina226_calibration_and_conversion():
    sensor = INA226(bus_num=1, address=0x40, shunt_ohms=0.010, max_expected_amps=8.0)
    assert sensor.calibration_value == 2097

    # Mock SMBus
    mock_bus = MagicMock()
    # REG_BUS_VOLTAGE (0x02) -> 12.0 V: 12.0 / 0.00125 = 9600 = 0x2580
    # REG_SHUNT_VOLTAGE (0x01) -> 5.0 mV: 5.0 / 0.0025 = 2000 = 0x07D0
    # REG_CURRENT (0x04) -> 0.5 A: 0.5 / sensor.current_lsb = 2048 = 0x0800
    # REG_POWER (0x03) -> 6.0 W: 6.0 / sensor.power_lsb = 983 = 0x03D7
    def mock_read_block(addr, reg, length):
        if reg == INA226.REG_BUS_VOLTAGE:
            return [0x25, 0x80]
        if reg == INA226.REG_SHUNT_VOLTAGE:
            return [0x07, 0xD0]
        if reg == INA226.REG_CURRENT:
            return [0x08, 0x00]
        if reg == INA226.REG_POWER:
            return [0x03, 0xD7]
        return [0x00, 0x00]

    mock_bus.read_i2c_block_data.side_effect = mock_read_block

    reading = sensor.read(mock_bus)
    assert reading.voltage_v == 12.0
    assert reading.shunt_mv == 5.0
    assert abs(reading.current_a - 0.5) < 0.01
    assert abs(reading.power_w - 6.0) < 0.05


def test_hardware_monitor_graceful_without_i2c():
    monitor = HardwareMonitor()
    with patch("aqua_gateway.hardware.monitor.SMBus", None):
        with patch.object(monitor, "read_cpu_temp", return_value=45.2):
            reading = monitor.read_all(force=True)
            assert reading.cpu_temp_c == 45.2
            assert reading.power_in is None
            assert reading.power_5v is None
            assert reading.rtc.status in ("unavailable", "ok", "power_lost")
            assert reading.available is True

            data = reading.as_dict()
            assert data["cpu_temp_c"] == 45.2
            assert data["power_in"] is None


def test_hardware_monitor_rtc_direct_i2c():
    monitor = HardwareMonitor()
    mock_bus = MagicMock()
    # MSB = 27 (0x1B), LSB = 0x80 (2 * 0.25 = 0.50 C) -> 27.5 C
    # Status reg (0x0F) -> 0x00 (OSF not set -> valid)
    def mock_read_byte(addr, reg):
        if reg == 0x11:
            return 27
        if reg == 0x12:
            return 0x80
        if reg == 0x0F:
            return 0x00
        return 0

    mock_bus.read_byte_data.side_effect = mock_read_byte

    with patch.object(monitor, "read_rtc_temp_sysfs", return_value=None):
        rtc = monitor.read_rtc(mock_bus)
        assert rtc.temp_c == 27.5
        assert rtc.valid is True
        assert rtc.status == "ok"


def test_hardware_monitor_caching():
    monitor = HardwareMonitor(cache_ttl_s=10.0)
    with patch.object(monitor, "read_cpu_temp", side_effect=[42.0, 55.0]):
        r1 = monitor.read_all(force=True)
        assert r1.cpu_temp_c == 42.0

        # Second call within TTL returns cached r1
        r2 = monitor.read_all(force=False)
        assert r2.cpu_temp_c == 42.0
        assert r2 is r1

        # Force call bypasses cache
        r3 = monitor.read_all(force=True)
        assert r3.cpu_temp_c == 55.0

