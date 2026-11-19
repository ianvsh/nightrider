# F9 OBD+ Sport DME protocol

Reverse-engineered from Focus 9 Technology's FocusOBD Windows app (decompiled
v1.11.407.1, v1.14.427.1 beta and v1.14.429 beta 2, downloaded from
https://www.ftech9.com/focusobd-software on 2026-10-11). Not tested against a
real DME yet.

## Link

- The DME's USB port is an FTDI USB-serial chip whose USB product string is
  `F9T OBD Interface` (Linux: `ftdi_sio` driver, `/dev/ttyUSB*`).
  The firmware updater also uses a separate `F9T MAF Controller` device.
- 2,000,000 baud, 8 data bits, no parity, 1 stop bit, RTS/CTS flow control.
- The FTDI CBUS pins put the DME's STM32 into bootloader mode for firmware
  updates. Never touch them (FTDI bit-bang mode 0x20) from Goldy.

## Framing

Plain ASCII.

- Request: `<CMD>\r` or `<CMD> <operand>\r`. CMD is two hex digits, e.g. `61\r`.
- Response: `#<CMD> <payload>\r`, e.g. `#61 845\r`. The app strips everything
  except `A-Z a-z 0-9 . -` and space, then splits on the single space.
- Requests are fire-and-forget. The app just sends requests in a loop and
  routes each response by its CMD; there is no checksum or sequence number.

## Commands

The **Payload** column shows what the DME sends back. "V" means a raw ADC
voltage (0-5.2 V; the app ignores anything above 5.2).

| CMD | Name | Payload | Conversion used by the app |
|---|---|---|---|
| 11 | Firmware version | `3.3.249` style | |
| 12 | Hardware/compatibility revision | string | |
| 13 | Vehicle model | `0` 944/951 (924S too), `1` 944 Spec, `2` 911 3.2 | |
| 14 | 5 V supply | V | |
| 15 | MAF enabled | flag | |
| 16 | Set vehicle model | (write) | don't use |
| 17 | DME reset count | integer | |
| 20 | MAF intake air temp | V | |
| 21 | Airflow meter signal | V | |
| 22 | DME supply voltage | V | x 3.26 = battery volts. Below 5 V the app treats the DME as off and blanks all values. |
| 23 | Intake air temp sensor | V | |
| 24 | Coolant temp sensor | V | |
| 25 | Altitude switch | V | |
| 26 | MAP sensor | V | GM 3-bar only: Pa = 62400 x V + 3600 - 500 (absolute). Not fitted on a stock NA car. |
| 27 | Idle / full-load switch | V | < 3.3 Idle, 3.3-4.1 Full load, >= 4.1 Off idle |
| 28 | Fuel quality switch | V | |
| 29 | Injector current | V | |
| 2A | Ignition current | V | |
| 2B | Barometric pressure | V | kPa = 18.75 x V + 30.625 |
| 2C | Narrowband O2 | V | |
| 2D | Wideband O2 input | V | depends on the wideband gauge (see below) |
| 2E | Aux input | V | |
| 2F | MAF airflow | V | |
| 31 | Battery voltage | V | defined but not polled by the app |
| 32 | Airflow volume | CFM | |
| 33 | Airflow mass | g/s | |
| 34 | Intake air temp | deg C | |
| 35 | Coolant temp | deg C | (cylinder head temp on a 911) |
| 36-38, 45-48 | MAF/AFM emulation values | | MAF conversion kit only |
| 41/42 (beta 2), 71/72 | Get/set MAF tuning table element | | write: don't use |
| 43 | MAF active trim index | | beta 2 only |
| 52 | 24/28-pin chip mode | | |
| 53 | Fuel pump | `ON` / `OFF` | |
| 54 | A/C compressor | `1` / `0` | |
| 55 | Coding plug | | |
| 56 | Narrowband O2 state | `0` rich, `3` lean, `1` switching | |
| 61 | Engine speed | integer RPM | |
| 62 | Ignition dwell | ms | |
| 63 | Injector pulse width | ms | |
| 64 | Ignition advance | `<dme>-<klr>` deg | KLR (knock) value is turbo-only (0 on the 924S, confirmed on a real drive 2026-10-14); retard = dme - klr, knock if > 1 deg. dme.py omits knock_retard_deg when KLR is 0 |
| 65 | Flywheel mark RPM | | |
| 66 | RPM sensor error count | integer | |
| 67 | Mark sensor error count | integer | |
| 68 | Flywheel tooth count | integer | 130 = 944 NA / 924S, 132 = 944 Turbo, 129 = 911 3.2. 0 while cranking = no flywheel signal. |
| 69 | Flywheel signal status | 4 chars of 0/1 | no mark, no reference, mark swapped, reference swapped |
| 6A/6B | RPM / mark pulse counters | | for waveform graphs only |
| 6D | KLR ignition advance | | |
| 6E | Idle control valve | ms | duty % = 14.9254 x ms - 34.3284 |
| 6F | Aux flex fuel | `<a>-<b>` | ethanol % = 149.993 - 7.49963 x a, fuel temp deg C = 41.25 x b - 81.25 |

## Wideband O2 conversions (CMD 2D)

| Gauge | AFR | Lambda |
|---|---|---|
| AEM 30-0300 | 2.375 V + 7.3125 (V < 0.3: heating; V > 4.5: invalid) | 0.1621 V + 0.499 |
| Autometer | 2.0 V + 10.0 | AFR / 14.68 |
| Autometer 2650-1828-77 | 1.6 V + 10.0 | AFR / 14.68 |
| ProSport Evo Premium | lambda x 14.68 | 0.136 V + 0.683 (the app shows this lambda as AFR too, a bug) |
| Innovate Motorsports | 3.008 V + 7.35 | 0.2 V + 0.5 |
| PLX | 2.0 V + 10.0 | AFR / 14.68 |
| Zeitronix Zt-2 (beta only) | 2.0 V + 9.6 | AFR / 14.68 |

## Smoothing in the app

The app averages most values over its last few readings (RPM 5, coolant 20,
air temp 10, DME volts 10, wideband 20, baro 200). Goldy uses raw values.
