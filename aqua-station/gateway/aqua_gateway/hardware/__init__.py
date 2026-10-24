"""Hardware monitoring for the Raspberry Pi gateway HAT.

Provides readings for:
* Vin (Input) and +5V_out power via INA226 sensors on I2C bus 1.
* RTC DS3231MZ temperature and health status.
* Raspberry Pi CPU core temperature.
"""

from .monitor import HardwareMonitor, HardwareReading, PowerReading, RtcReading

__all__ = ["HardwareMonitor", "HardwareReading", "PowerReading", "RtcReading"]

