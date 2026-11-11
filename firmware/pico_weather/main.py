# main.py - pico-weather firmware for the FSO observatory weather logger.
#
# MicroPython on a Raspberry Pi Pico W. Save on the Pico as main.py so it
# starts at power-up. Prints newline-terminated ASCII on USB serial:
#
#   # pico-weather v3.2 wind=10Hz anemo=4-20mA shunt=165.0ohm range=30.0m/s zero=3.72mA light=ldr baro=BME280 dht=yes ...
#   W,t_ms,v_adc,i_ma,wind_ms,clip                          10 Hz
#   E,t_ms,dht_c,dht_rh,baro_hpa,baro_c,light_v,light_x,baro_rh   1 Hz
#
# baro_rh (v3.1+) is BME280 relative humidity; empty with a BMP280.
# An empty field means the sensor is missing or the read failed (never 0).
# The header is repeated every HEADER_EVERY_S so a host that connects late
# still receives it. Other "#" lines are informational; "# warning ..." lines
# report a wind signal frozen at a fixed level (the sensor is not measuring).
#
# Wiring (see the FSO observatory schematic):
#   GP26 / ADC0  WIND   4-20 mA loop across 2 x 330 ohm in parallel (165 ohm)
#   GP28 / ADC2  LIGHT  LDR from 3V3, R_LIGHT to AGND (brighter -> higher V)
#   GP20         SDA    BMP280/BME280, address 0x76 (SDO to GND)
#   GP21, GP17   SCL and DHT11 data; detected at start-up, so either way round works

import time
import struct
import machine
from machine import ADC, I2C, Pin

VERSION = "v3.2"
ANEMO_RANGE = 30.0      # m/s over the 16 mA span
# Loop current this circuit reads with the cups at rest. Measured 3.72 mA (0.614 V)
# on 22 Oct, 29 Oct and 10 Nov 2026, not the nominal 4.00 mA; with 4.00 the first
# 0.5 m/s read as zero. To re-measure: hold the cups still for a minute and take the
# mean i_ma of the W lines. Nothing is zeroed automatically at start-up.
ANEMO_ZERO_MA = 3.72
ANEMO_SPAN_MA = 16.0
SHUNT_OHM = 165.0       # 2 x 330 ohm in parallel
LIGHT_MODE = "ldr"      # "ldr" or "bpw34"
R_LIGHT = 1000.0        # ohm, divider/load resistor on LIGHT
VREF = 3.3              # ADC_VREF is not connected: the ADC uses the 3V3 rail
WIND_PIN = 26
LIGHT_PIN = 28
SDA_PIN = 20
SCL_DHT_PINS = (21, 17)  # schematic: SCL=GP21, DHT=GP17
BARO_ADDR = 0x76
ADC_SAMPLES = 16        # averaged per reading
CLIP_RAW = 65200        # raw u16 at or above this is treated as the ADC rail
LOOP_FAULT_MA = 3.2     # below this the loop is open or unpowered: wind empty
FROZEN_WINDOW = 600     # W samples (60 s) per frozen-signal check
FROZEN_SD_V = 0.006     # ADC noise alone is about 0.003 V
FROZEN_ABOVE_MA = 1.0   # only when this far above zero: a still anemometer is flat too
WIND_PERIOD_MS = 100
ENV_EVERY = 10          # E line every 10th W line (1 Hz)
HEADER_EVERY_S = 60


def fmt(value, digits):
    return "" if value is None else "%.*f" % (digits, value)


def read_adc(adc):
    total, clipped = 0, False
    for _ in range(ADC_SAMPLES):
        raw = adc.read_u16()
        total += raw
        clipped = clipped or raw >= CLIP_RAW
    return total / ADC_SAMPLES * VREF / 65535, clipped


class Baro:
    """BMP280 (chip id 0x58) or BME280 (0x60, which also measures humidity)."""

    def __init__(self, i2c):
        self.i2c = i2c
        chip = i2c.readfrom_mem(BARO_ADDR, 0xD0, 1)[0]
        self.name = {0x58: "BMP280", 0x60: "BME280"}.get(chip)
        if self.name is None:
            raise OSError("unknown barometer chip id 0x%02x" % chip)
        self.cal = struct.unpack("<HhhHhhhhhhhh", i2c.readfrom_mem(BARO_ADDR, 0x88, 24))
        self.humidity = chip == 0x60
        if self.humidity:
            h1 = i2c.readfrom_mem(BARO_ADDR, 0xA1, 1)[0]
            e = i2c.readfrom_mem(BARO_ADDR, 0xE1, 7)
            self.hcal = (h1, struct.unpack("<h", e[0:2])[0], e[2],
                         (struct.unpack("b", e[3:4])[0] << 4) | (e[4] & 0x0F),
                         (struct.unpack("b", e[5:6])[0] << 4) | (e[4] >> 4),
                         struct.unpack("b", e[6:7])[0])
            i2c.writeto_mem(BARO_ADDR, 0xF2, b"\x01")   # humidity x1; must precede ctrl_meas
        i2c.writeto_mem(BARO_ADDR, 0xF5, b"\x20")       # standby 62.5 ms, IIR filter off
        i2c.writeto_mem(BARO_ADDR, 0xF4, b"\x4f")       # temp x2, pressure x4, normal mode

    def read(self):
        """Return (pressure hPa, temperature degC, RH %) with the Bosch integer compensation."""
        T1, T2, T3, P1, P2, P3, P4, P5, P6, P7, P8, P9 = self.cal
        d = self.i2c.readfrom_mem(BARO_ADDR, 0xF7, 8 if self.humidity else 6)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        if adc_t == 0x80000:
            return None, None, None                      # measurement skipped / not ready
        v1 = (((adc_t >> 3) - (T1 << 1)) * T2) >> 11
        v2 = (((((adc_t >> 4) - T1) * ((adc_t >> 4) - T1)) >> 12) * T3) >> 14
        t_fine = v1 + v2
        temp_c = ((t_fine * 5 + 128) >> 8) / 100
        rh = None
        if self.humidity and ((d[6] << 8) | d[7]) != 0x8000:
            H1, H2, H3, H4, H5, H6 = self.hcal
            x = t_fine - 76800
            x = (((((((d[6] << 8) | d[7]) << 14) - (H4 << 20) - (H5 * x)) + 16384) >> 15) *
                 (((((((x * H6) >> 10) * (((x * H3) >> 11) + 32768)) >> 10) + 2097152) * H2 + 8192) >> 14))
            x = x - (((((x >> 15) * (x >> 15)) >> 7) * H1) >> 4)
            rh = (max(0, min(x, 419430400)) >> 12) / 1024
        pressure = None
        v1 = t_fine - 128000
        v2 = v1 * v1 * P6 + ((v1 * P5) << 17) + (P4 << 35)
        v1 = ((v1 * v1 * P3) >> 8) + ((v1 * P2) << 12)
        v1 = (((1 << 47) + v1) * P1) >> 33
        if v1 != 0 and adc_p != 0x80000:
            p = 1048576 - adc_p
            p = (((p << 31) - v2) * 3125) // v1
            v1 = (P9 * (p >> 13) * (p >> 13)) >> 25
            v2 = (P8 * p) >> 19
            pressure = (((p + v1 + v2) >> 8) + (P7 << 4)) / 25600
        return pressure, temp_c, rh


def find_baro():
    """Try SCL on each candidate pin; the other one is the DHT11 data line."""
    for attempt in range(3):
        for scl in SCL_DHT_PINS:
            try:
                i2c = I2C(0, sda=Pin(SDA_PIN), scl=Pin(scl), freq=100000)
                if BARO_ADDR in i2c.scan():
                    return Baro(i2c), scl
            except OSError:
                pass
            # Clocking the DHT11 line can start a DHT11 reply; detach the pin
            # and let that finish before the next attempt.
            Pin(scl, Pin.IN)
            time.sleep_ms(50)
    Pin(SDA_PIN, Pin.IN)
    return None, None


def read_dht11(pin):
    """Return (temperature degC, RH %) as integers.

    Replaces dht.DHT11.measure(), which blocks for about 270 ms (a fixed
    250 ms wait) and would stall the 10 Hz wind stream. This takes ~25 ms.
    """
    pin.init(Pin.OUT, value=0)
    time.sleep_ms(20)                       # start signal: low for >= 18 ms
    pulses = []
    irq = machine.disable_irq()             # about 5 ms with interrupts off
    try:
        pin.init(Pin.IN, Pin.PULL_UP)
        for _ in range(41):                 # 80 us response pulse, then 40 data bits
            pulses.append(machine.time_pulse_us(pin, 1, 200))
    finally:
        machine.enable_irq(irq)
    if min(pulses) < 0:
        raise OSError("DHT11 timeout")
    data = bytearray(5)
    for i, width in enumerate(pulses[1:]):
        if width > 48:                      # 26-28 us = 0, 70 us = 1
            data[i // 8] |= 0x80 >> (i % 8)
    if (data[0] + data[1] + data[2] + data[3]) & 0xFF != data[4]:
        raise OSError("DHT11 checksum")
    return data[2], data[0]


def wind_from_volts(v):
    """(loop current mA, wind m/s or None) from the shunt voltage."""
    i_ma = v / SHUNT_OHM * 1000
    if i_ma < LOOP_FAULT_MA:
        return i_ma, None
    return i_ma, max(0.0, (i_ma - ANEMO_ZERO_MA) / ANEMO_SPAN_MA * ANEMO_RANGE)


class FrozenCheck:
    """Flags a wind signal that sits at one level well above zero for a whole window.

    Real wind always fluctuates. After some power-ups this sensor has held a fixed
    current (9.2 and 13.3 mA seen, flat to 3 mV), which would be logged as steady wind.
    """

    def __init__(self):
        self.reset(0.0)

    def reset(self, origin):
        self.n, self.total, self.squares, self.origin = 0, 0.0, 0.0, origin

    def add(self, v):
        """Add one sample; returns a warning string at the end of a frozen window, else None."""
        if self.n == 0:
            self.origin = v  # Sum deviations from the first sample: small numbers for 32-bit floats
        d = v - self.origin
        self.n += 1
        self.total += d
        self.squares += d * d
        if self.n < FROZEN_WINDOW:
            return None
        mean = self.total / self.n
        sd = max(0.0, self.squares / self.n - mean * mean) ** 0.5
        i_ma = (self.origin + mean) / SHUNT_OHM * 1000
        self.reset(0.0)
        if sd < FROZEN_SD_V and i_ma > ANEMO_ZERO_MA + FROZEN_ABOVE_MA:
            return "# warning wind_frozen level=%.2fmA sd=%.4fV over %ds: sensor not measuring, re-plug or power-cycle it" % (
                i_ma, sd, FROZEN_WINDOW * WIND_PERIOD_MS // 1000)
        return None


def header(baro, scl, dht_pin):
    return ("# pico-weather %s wind=%dHz anemo=4-20mA shunt=%.1fohm range=%.1fm/s zero=%.2fmA light=%s r_light=%.0fohm "
            "baro=%s dht=%s pins=wind:GP%d,light:GP%d,sda:GP%d,scl:%s,dht:GP%d"
            % (VERSION, 1000 // WIND_PERIOD_MS, SHUNT_OHM, ANEMO_RANGE, ANEMO_ZERO_MA, LIGHT_MODE, R_LIGHT,
               baro.name if baro else "none", "yes", WIND_PIN, LIGHT_PIN, SDA_PIN,
               "GP%d" % scl if scl is not None else "none", dht_pin))


def main():
    wind_adc, light_adc = ADC(Pin(WIND_PIN)), ADC(Pin(LIGHT_PIN))
    led = Pin("LED", Pin.OUT)
    baro, scl = find_baro()
    dht_pin = SCL_DHT_PINS[1] if scl == SCL_DHT_PINS[0] else SCL_DHT_PINS[0]
    sensor = Pin(dht_pin, Pin.IN, Pin.PULL_UP)
    head = header(baro, scl, dht_pin)
    print(head)
    if baro is None:
        print("# barometer not found at 0x%02x on SDA GP%d with SCL on GP%d or GP%d" % ((BARO_ADDR, SDA_PIN) + SCL_DHT_PINS))
    elif scl != SCL_DHT_PINS[0]:
        print("# note: SCL found on GP%d and DHT11 on GP%d (swapped relative to the schematic)" % (scl, dht_pin))

    tick = 0
    frozen = FrozenCheck()
    next_ms = time.ticks_ms()
    last_header = time.ticks_ms()
    while True:
        wait = time.ticks_diff(next_ms, time.ticks_ms())
        if wait > 0:
            time.sleep_ms(wait)
        now = time.ticks_ms()
        next_ms = time.ticks_add(next_ms, WIND_PERIOD_MS)
        if time.ticks_diff(now, next_ms) > 1000:
            next_ms = time.ticks_add(now, WIND_PERIOD_MS)   # resynchronise after a stall

        v, clipped = read_adc(wind_adc)
        i_ma, wind = wind_from_volts(v)
        print("W,%d,%.4f,%.3f,%s,%d" % (now, v, i_ma, fmt(wind, 3), 1 if clipped else 0))
        warning = frozen.add(v)
        if warning:
            print(warning)

        tick += 1
        if tick % ENV_EVERY:
            continue
        led.toggle()
        dht_c = dht_rh = None
        try:
            dht_c, dht_rh = read_dht11(sensor)
            if dht_rh == 0:
                dht_rh = None   # DHT11 cannot measure below about 20 %RH; 0 means out of range
        except OSError:
            pass
        baro_hpa = baro_c = baro_rh = None
        if baro is None and tick % (ENV_EVERY * 30) == 0:
            baro, scl = find_baro()   # retry every 30 s if it was missing at boot
        if baro is not None:
            try:
                baro_hpa, baro_c, baro_rh = baro.read()
            except OSError:
                baro = None
        light_v, _ = read_adc(light_adc)
        if LIGHT_MODE == "bpw34":
            light_x = light_v / R_LIGHT * 1e6                                   # photocurrent, uA
        else:
            light_x = R_LIGHT * (VREF - light_v) / light_v if light_v > 0.005 else None  # LDR ohm
        print("E,%d,%s,%s,%s,%s,%.3f,%s,%s" % (now, "" if dht_c is None else "%d" % dht_c,
                                                "" if dht_rh is None else "%d" % dht_rh,
                                                fmt(baro_hpa, 2), fmt(baro_c, 2), light_v, fmt(light_x, 1),
                                                fmt(baro_rh, 1)))
        if time.ticks_diff(now, last_header) >= HEADER_EVERY_S * 1000:
            last_header = now
            print(head)


if __name__ == "__main__":  # True when MicroPython runs main.py at power-up
    main()
