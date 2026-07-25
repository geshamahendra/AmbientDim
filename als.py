import asyncio
from winrt.windows.devices.sensors import LightSensor

async def main():
    sensor = LightSensor.get_default()
    if sensor is None:
        print("No ALS found")
        return
    sensor.report_interval = 1000
    reading = sensor.get_current_reading()
    print(f"Lux: {reading.illuminance_in_lux}")

asyncio.run(main())