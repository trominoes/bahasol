# BahaSol Pump Controller — Pseudo-code
### ESP32 + VFD + Inverter via shared RS-485 bus | Rev 0.2 | April 2026

---

## SECTION A — Simplified Operational Overview

This section describes what the controller does in plain language. Every rule below
maps directly to logic in the algorithm sections that follow.

**Running without a timer**
The timer is optional. By default (or by rotating the encoder counterclockwise past
the minimum), the timer is set to OFF (display shows `----`). In this mode, pressing
ON starts the pump and it runs indefinitely until you press OFF. This is the simplest
way to operate the system — no setup required.

**Setting a timer (optional)**
Turn the rotary encoder clockwise to dial in a desired run time. The display blinks
the current value while you adjust it and continues blinking after you stop — this
is intentional. A blinking timer means the value is pending but not yet committed.
The encoder advances in stepped increments that coarsen at longer durations: 1-minute
steps from 1–10 min, 10-minute steps from 10–60 min, and 30-minute steps from 60 min
up to 18 hours. This makes short precision runs easy to set and long runs quick to
reach without excessive scrolling. The timer can be changed any time the pump is off.
If a timer is set, the pump stops automatically when the countdown reaches zero.

**Encoder while the pump is running**
Turning the rotary encoder while the pump is running has no effect. The timer is
committed at the moment ON is pressed and cannot be adjusted mid-run. To change the
run time, press OFF, adjust the encoder, then press ON again.

**Starting the pump**
Press ON. If there is enough power available (see power rules below), the pump starts
and the timer value is committed. The display immediately goes from blinking to steady,
indicating the transition from "pending" to "active." If a timer is set, the display
shows the remaining time counting down. If no timer is set (`----`), the display
continues cycling through sensor readings. If there is not enough power, the pump does
not start and the NeoPixels indicate the reason.

**Stopping the pump**
Press OFF (quick press) at any time to stop the pump immediately. If a timer is
active, it is cancelled. The pump also stops automatically when a countdown timer
reaches zero.

**Speed control**
The potentiometer sets pump speed continuously. Its minimum position corresponds to
the minimum safe VFD frequency (lowest emitter pressure), not zero. Its maximum
corresponds to the design operating frequency. Adjustments take effect immediately
while the pump is running.

**Power rules**
The pump runs if at least one of these conditions is true:
  1. Solar power alone is enough to meet the pump's demand (battery SoC is irrelevant
     in this case — the pump runs on sunshine regardless of battery state).
  2. Solar power is insufficient on its own, but the battery is above its discharge
     floor AND the sun is up (solar > night guard threshold), so the two together
     can supply the load.

The pump stops (or will not start) if:
  — Solar alone is insufficient AND the battery is at or below its discharge floor.
  — The sun has set and there is no solar to supplement (night guard: prevents
    draining the battery overnight so it can recharge the next morning).
  — A fault condition is active (see Section B).

**Clearing a fault**
When a fault is active, the NeoPixels flash and the display shows the fault code.
A quick press of OFF stops the pump but does not clear the fault — this is intentional,
so the operator sees the fault code and can diagnose it before continuing.
To acknowledge and clear a fault, hold OFF for 3 seconds. The controller checks
that the fault condition is actually resolved (e.g., VFD is responsive, sensor data
is fresh) before clearing. If the condition is still present, the fault remains.
There is no button combination beyond this long-press; the two momentary switches
are the only user controls.

**After the timer completes**
When the countdown reaches zero and the pump stops automatically, the display
shows blinking "r 00" for 30 seconds (configurable) as a visual confirmation —
the oven-beep equivalent. After that period, the display returns to cycling through
sensor readings. If no button or encoder is touched, the display will dim and turn
off after 5 minutes of inactivity to conserve the 5V rail. Any button press or
encoder turn wakes the display immediately.

**Power interrupted mid-run**
If solar and battery power fall below the threshold while a timed run is in progress,
the pump stops but the system enters an INTERRUPTED state rather than simple idle.
The remaining time continues to blink on the display (paused, not counting), and the
NeoPixels reflect the actual power conditions — battery and solar pixels will show
their true state, indicating why the pump stopped. When you press ON again, if power
has recovered, the pump restarts and the countdown resumes from the remaining time.
This state persists through light sleep cycles (RAM is retained) and through a hard
power cycle (saved to flash before sleeping).

**Sleep behavior**
The ESP32 enters light sleep 10 minutes after the last button press, encoder turn,
or state change. This keeps the display active long enough to be useful after a pump
run, without leaving the system fully awake overnight. The ESP32 wakes immediately on
any button press or encoder edge, and also wakes once per minute to read sensors and
write a log record. On wake, state and display restore exactly as they were before
sleep. Deep sleep is not used — see Section K for the full reasoning.

---

## SECTION B — Failure Modes and System Responses

Each failure mode lists how it is detected, how the system responds, and what the
NeoPixel strip shows. Faults marked [AUTO-CLEAR] resolve on their own once the
condition improves. Faults marked [MANUAL-CLEAR] require the operator to press OFF
to acknowledge before the system will attempt to restart.

---

### B.1 Electrical / Power Failures

**Power loss or brownout (inverter/battery system goes offline)**
Detection: Battery SoC read returns 0 or out-of-range; RS-485 bus goes silent.
Response: Pump is commanded OFF (VFD holds last command, but inverter output will
  collapse anyway). System attempts VFD STOP command; if no ACK, it logs the failure
  and enters FAULT_POWER state. On power restoration, system re-initialises from
  saved state in flash.
NeoPixels: All pixels RED, fast blink.
Recovery: [AUTO-CLEAR] — clears automatically on successful sensor and VFD handshake
  after power returns.

**Overnight battery drain from inverter standby**
Detection: This is a hardware/wiring concern, not a software-detectable event, but
  it is the primary overnight drain risk. The ESP32 control system itself in light
  sleep draws ~30–50 mA at 5V (≈ 0.2 W), which over 12 hours consumes roughly 2 Wh
  — negligible against a 2 kWh battery. However, if the 5V PSU is powered from the
  inverter's AC output bus, the inverter must remain on all night to keep the
  controller alive. Most hybrid inverters draw 15–50 W in standby just for their
  own control board and display. At 30 W for 12 hours that is 360 Wh — 18% of the
  battery — which can push the battery below the 10% discharge floor by morning.
Response: Power the 5V PSU directly from the 48V DC battery bus using a small
  48V→5V step-down (buck) converter rated at ~1 A. This bypasses the inverter
  entirely for the control system, allowing the inverter to enter its own ECO/sleep
  mode overnight. The actual control electronics overnight drain then becomes ~2 Wh,
  which is inconsequential. Verify whether your inverter model has a configurable
  ECO or sleep threshold.
NeoPixels: Not directly indicated (the battery SoC pixels will show the consequence
  by morning if the drain is severe enough).

**Surge or lightning strike**
Detection: Indirect — observed as VFD TRIP or RS-485 comms failure after the event.
Response: Same as VFD_COMMS fault (see B.3). TVS diodes and MOVs on the RS-485
  lines and input supply are a hardware mitigation; the software cannot prevent
  damage, only detect it.
Note: Proper surge protection on the Cat5e cable entry point is a build requirement.

---

### B.2 Pump / Mechanical Failures

**Pump overheating**
Detection: No direct temperature sensor is assumed in the current BOM. Indirect proxy:
  VFD output current rises above rated value (many VFDs expose a current register via
  Modbus). If VFD reports overcurrent sustained > 30 s at fixed speed, log a thermal
  warning.
Response: Reduce VFD frequency by one step (5 Hz) and wait 60 s. If overcurrent
  persists, stop pump and raise FAULT_OVERCURRENT.
NeoPixels: Pixel 7 ORANGE slow blink (warning) → RED fast blink (fault).
Recovery: [MANUAL-CLEAR].

**Cavitation (pump running dry or air entrainment)**
Detection: Indirect — cavitation causes a characteristic drop in VFD output current
  (the pump loses hydraulic load). If current drops below a low-current threshold
  while the pump is running, flag a cavitation warning.
Response: Log a WARN_CAVITATION event. After two consecutive minutes below threshold,
  stop the pump and raise FAULT_CAVITATION to protect the pump from dry-run damage.
  Note: A pressure sensor or flow meter would make this detection precise; without
  one, current-based inference is a heuristic.
NeoPixels: Pixel 7 YELLOW slow blink (warning) → ORANGE fast blink (fault, stop).
Recovery: [MANUAL-CLEAR].

**Mechanical seizure or blockage**
Detection: VFD trips on overcurrent immediately at startup (locked rotor). VFD
  reports a TRIP fault code in its status register.
Response: Stop commands are already moot (VFD has tripped). Log FAULT_VFD_TRIP with
  the VFD fault code. Do not attempt automatic restart.
NeoPixels: All pixels RED, fast blink.
Recovery: [MANUAL-CLEAR] after operator inspects pump mechanically.

**Speed below minimum emitter pressure (underflow)**
Detection: Speed setpoint (from potentiometer) maps below VFD_MIN_HZ.
Response: Clamp commanded frequency to VFD_MIN_HZ. This is enforced in software
  at the mapping stage — the pump can never be commanded below the safe lower limit.
  No fault state; this is normal clamping behavior.
NeoPixels: No change. Potentiometer position is silently floored.

**Speed above maximum emitter pressure (overflow / burst risk)**
Detection: Speed setpoint maps above VFD_MAX_HZ (pot at full travel).
Response: Clamp commanded frequency to VFD_MAX_HZ. Same clamping logic.
NeoPixels: No change.

---

### B.3 Communication Failures

**VFD RS-485 / Modbus failure**
Detection: No valid Modbus response within VFD_COMMS_TIMEOUT_MS, repeated
  VFD_FAULT_RETRY_COUNT times consecutively.
Response: Log FAULT_VFD_COMMS. If pump was running, the VFD retains the last
  commanded state (running). Operator must physically inspect. System will not
  attempt further VFD commands until fault is cleared.
NeoPixels: Pixel 7 RED, fast blink.
Recovery: [MANUAL-CLEAR].

**Inverter RS-485 / Modbus failure (loss of SoC and solar data)**
Detection: No valid Modbus response from inverter address within timeout.
Response: Log WARN_INVERTER_COMMS. Do not stop the pump immediately — use the
  last known SoC and solar power values for up to SENSOR_STALE_TIMEOUT_S (e.g.,
  120 s). After that, conservatively assume battery is at minimum SoC and solar
  is zero. If pump was running and power sufficiency can no longer be confirmed,
  stop the pump and raise FAULT_NO_SENSOR_DATA.
NeoPixels: Pixel 7 ORANGE slow blink (stale data warning) → RED (fault, stopped).
Recovery: [AUTO-CLEAR] when inverter comms resume.

**RS-485 bus collision (inverter and VFD address conflict)**
Detection: Garbled or unexpected responses to Modbus queries (CRC errors).
Response: Retry with a back-off delay. If CRC errors persist, log FAULT_BUS_COLLISION
  and halt all RS-485 traffic. Operator must verify that inverter and VFD have
  distinct, non-overlapping Modbus slave addresses before clearing.
Note: Assign VFD address = 1, inverter address = 2 (or per manufacturer default).
  Confirm during commissioning that both respond correctly to individual queries
  before running the main loop.
Recovery: [MANUAL-CLEAR].

---

### B.4 Environmental / Hardware Drift

**Insufficient weatherization (moisture ingress)**
Detection: Cannot be detected in software. This is a hardware/installation concern.
  Symptom: erratic sensor readings, RS-485 errors, or sudden ESP32 resets may all
  indicate moisture ingress into the junction box.
Response: Erratic reads will surface as sensor stale warnings or comms faults (B.3).
  Log all anomalous events so post-mortem analysis is possible.
Mitigation: Rated IP65 or higher enclosure, cable glands torqued fully, silica gel
  desiccant inside box, inspect seals annually.

**Potentiometer drift (ADC noise, aging wiper)**
Detection: Speed setpoint jitter when pot is held still (measured as variance in
  ADC readings exceeding a deadband threshold over a 1-second window).
Response: Apply a deadband filter: only update the commanded VFD frequency if the
  new reading differs from the current setpoint by > SPEED_DEADBAND_HZ (e.g., 1 Hz).
  This prevents continuous micro-adjustments to the VFD.
NeoPixels: No indication (transparent to user).

**Timer drift (RTC crystal aging)**
Detection: DS3231 has a specified accuracy of ±2 ppm (< 1 minute/year). Not a
  practical concern for daily irrigation. No active detection needed.
Mitigation: If internet access is ever available (WiFi), add an NTP sync step on
  boot. Otherwise, accept the DS3231's accuracy as sufficient.

**RTC failure (I2C comms)**
Detection: I2C read of DS3231 returns all-zeros or invalid BCD values.
Response: Log FAULT_RTC. Timer function is impaired — elapsed time tracking falls
  back to the ESP32 internal millisecond counter (accurate for hours-long runs,
  but drifts over days without the RTC). NeoPixels show a warning; pump operation
  continues using millis() for the countdown.
NeoPixels: Pixel 6 YELLOW slow blink.
Recovery: [AUTO-CLEAR] if RTC responds correctly on next read.

**RTC coin cell exhausted**
Detection: DS3231 I2C reads succeed (chip is alive), but the reported year is
  before 2024 — the chip has reset to its epoch date (Jan 1 2000) because the
  CR2032 backup cell died and the chip lost power between sessions.
Response: Log WARN_RTC_BATTERY. Display cycles normally but pixel 6 blinks YELLOW
  as a persistent reminder. Time-of-day display will be wrong until the cell is
  replaced and time is re-set. The countdown timer still works correctly because it
  measures elapsed milliseconds from ON press, not wall-clock time.
Mitigation: CR2032 cells on DS3231 modules last 5–10 years under normal use.
  Inspect and replace annually as part of seasonal maintenance.
NeoPixels: Pixel 6 YELLOW slow blink (same as RTC comms fault; the fault code
  on the 7-seg, F07 vs. F08, distinguishes the two).
Recovery: [MANUAL-CLEAR] after replacing the coin cell and setting the correct time
  via a brief firmware flash or serial command.

---

### B.5 Irrigation Shortfall

**Shortfall due to late start by operator**
Detection: Not a fault — this is a user decision. The controller does not enforce
  a start time. The timer simply runs for however long the operator set.
Mitigation: Operator education: total daily water requirement from the irrigation
  schedule analysis (module 4) informs how many minutes the pump must run.

**Shortfall due to intermittent cloud cover**
Detection: Solar power drops below PUMP_POWER_KW during a run; battery supplements.
  If battery also drops to floor during a cloudy period, the pump stops mid-session.
Response: Log the stop event with failure_reason = 'INSUFFICIENT_POWER'. NeoPixels
  indicate low power. When cloud clears and solar recovers, the pump does NOT
  auto-restart — the operator must press ON again. This is intentional: the
  operator should confirm conditions before re-starting rather than having the
  pump cycle on and off unattended.
NeoPixels: Pixels 2-3 YELLOW (marginal solar) → pump stops, pixels 0-1 go to idle.

**Battery depleted without solar recovery (extended overcast)**
Detection: battery_soc_pct ≤ BATTERY_MIN_SOC_PCT AND solar_power_kw < PUMP_POWER_KW.
Response: Pump will not start (or stops if running). Log WARN_LOW_BATTERY.
  System continues to poll inverter and log SoC while in sleep. No pump commands
  are issued until conditions improve and operator presses ON.
NeoPixels: Pixels 4-5 RED fast blink. Pixel 7 ORANGE slow blink.
Recovery: [AUTO-CLEAR condition] — battery recovers on its own. But pump restart
  still requires operator to press ON.

---

### B.6 Firmware / Software Faults

**ESP32 crash or hang (watchdog reset)**
Detection: Hardware watchdog timer fires if the main loop does not call feed_watchdog()
  within WATCHDOG_INTERVAL_MS. This causes an automatic CPU reset.
Response: On reset, the boot sequence runs, persistent state is loaded from flash
  (total run hours, last known config), and the pump is confirmed stopped via VFD
  command before resuming normal operation.
NeoPixels: Brief all-white flash on boot (visible restart indicator).
Recovery: [AUTO-CLEAR] — automatic restart.

**Flash storage corruption (LittleFS error)**
Detection: Filesystem mount fails or file read returns invalid data.
Response: Log to serial output only. Re-format filesystem (losing historical logs),
  reset total_run_hours to 0, and continue with default config values. Flag
  WARN_STORAGE_RESET on the display.
NeoPixels: Pixel 7 YELLOW slow blink for 60 s after reset.

---

## SECTION C — Hardware Interface Definitions

```
# ── Physical inputs ──────────────────────────────────────────────────────────
POT_PIN         : analog in   → 10K potentiometer (speed setpoint)
                                Min position = VFD_MIN_HZ, max = VFD_MAX_HZ
                                Never commanded to 0 in software

ENC_A, ENC_B    : digital in  → Rotary encoder, interrupt-driven
                                Rotation sets timer duration when pump is OFF

SW_ON           : digital in  → Momentary pushbutton, active-low
                                Press: start pump + begin countdown (if power OK)

SW_OFF          : digital in  → Momentary pushbutton, active-low
                                Press: stop pump immediately; also clears FAULT

# ── RS-485 shared bus (inverter + VFD, distinct Modbus addresses) ────────────
RS485_TX, RS485_RX : UART2    → MAX3485 module
RS485_DE_RE        : digital out → MAX3485 driver-enable (HIGH = transmit)
#   Inverter Modbus address  = 2  (reads: battery SoC %, solar power kW)
#   VFD Modbus address       = 1  (writes: run/stop, frequency setpoint)
#   120Ω termination resistor fitted at the far end of the Cat5e run
#   Both devices must be confirmed at distinct addresses during commissioning

# ── Display & indicators ──────────────────────────────────────────────────────
SEG7_CLK, SEG7_DIO : TM1637 driver → 4-digit 7-segment display
NEOPIXEL_PIN       : digital out   → NeoPixel strip (WS2812B), N pixels

# ── Timekeeping ───────────────────────────────────────────────────────────────
RTC_SDA, RTC_SCL   : I2C shared bus → DS3231 module
#   DS3231 keeps time independently while ESP32 is in light sleep
```

---

## SECTION D — System Constants & Configuration

These values are set at compile time. Configuration changes require a firmware
reflash. Keeping them hard-coded (rather than in a menu) reduces failure points
and keeps the user interface minimal.

```python
# ── Electrical / pump ────────────────────────────────────────────────────────
PUMP_POWER_KW           = 1.263   # AC draw at design operating point [kW]
VFD_MIN_HZ              = 25.0    # Minimum safe frequency (sets min emitter pressure)
VFD_MAX_HZ              = 60.0    # Maximum frequency (sets max emitter pressure)
VFD_RAMP_TIME_S         = 5       # Soft-start ramp time written to VFD once at init
VFD_MODBUS_ADDR         = 1       # VFD slave address
INVERTER_MODBUS_ADDR    = 2       # Inverter slave address

# ── Battery / power protection ────────────────────────────────────────────────
BATTERY_MIN_SOC_PCT     = 10.0    # Discharge floor: battery-assisted runs stop here
MIN_SOLAR_FOR_DISCHARGE = 0.10    # [kW] Night guard threshold: if solar < this,
                                  # treat as night — no battery discharge permitted.
                                  # Solar-only runs (Case A) are still allowed at
                                  # any SoC as long as solar >= PUMP_POWER_KW.

# ── Timer ─────────────────────────────────────────────────────────────────────
TIMER_OFF               = 0       # Sentinel: 0 = no timer; pump runs until OFF pressed
TIMER_MIN_MINUTES       = 1       # Smallest non-zero timer value
TIMER_MAX_MINUTES       = 1080    # 18 hours; pump stops on power fault before this
TIMER_DEFAULT_MINUTES   = TIMER_OFF   # Default at power-on: no timer

# Encoder step sizes (logarithmic progression):
#   1  min steps for  1 – 10 min  (fine control for short runs)
#   10 min steps for 10 – 60 min  (medium runs)
#   30 min steps for 60 – 1080 min (long runs, up to 18 h)
# Counterclockwise past 1 min wraps to TIMER_OFF (display "----")
# Clockwise from TIMER_OFF jumps directly to 1 min on first click

# ── Speed control ─────────────────────────────────────────────────────────────
SPEED_DEADBAND_HZ       = 1.0     # Ignore pot changes smaller than this [Hz]

# ── Communication ─────────────────────────────────────────────────────────────
VFD_COMMS_TIMEOUT_MS    = 2000    # Max wait for VFD Modbus ACK
INV_COMMS_TIMEOUT_MS    = 2000    # Max wait for inverter Modbus response
VFD_FAULT_RETRY_COUNT   = 3       # Consecutive failures before FAULT_VFD_COMMS
SENSOR_STALE_TIMEOUT_S  = 120     # Use last-known sensor values for this long
                                  # before treating them as unreliable

# ── Fault / protection thresholds ─────────────────────────────────────────────
OVERCURRENT_THRESHOLD_A = 3.0     # VFD output current above which overtemp
                                  # warning is raised (verify against pump FLA)
OVERCURRENT_DURATION_S  = 30      # Seconds of sustained overcurrent before response
LOW_CURRENT_THRESHOLD_A = 0.3     # Below this while running → cavitation warning
LOW_CURRENT_DURATION_S  = 120     # Seconds before cavitation fault is raised

# ── Logging & sleep ───────────────────────────────────────────────────────────
LOG_INTERVAL_S          = 60      # Write a sensor+state record every N seconds
LOG_ON_STATE_CHANGE     = True    # Also write on any pump state transition
SLEEP_DELAY_S           = 600     # Seconds of inactivity before entering light sleep
                                  # (10 minutes; resets on any button/encoder event)
SLEEP_WAKE_INTERVAL_S   = 60      # Light sleep wakes every N s for sensor poll + log
WATCHDOG_INTERVAL_MS    = 30000   # Hardware watchdog feed interval

# ── Display ───────────────────────────────────────────────────────────────────
DISPLAY_CYCLE_S         = 3       # Seconds per sensor page in idle cycling
TIMER_BLINK_HZ          = 2       # Blink rate for pending or paused timer value
DONE_DISPLAY_S          = 30      # Seconds to show blinking "r 00" after timer completes
DISPLAY_TIMEOUT_S       = 300     # Seconds of inactivity before display dims to off
                                  # (must be ≤ SLEEP_DELAY_S; display turns off first,
                                  #  then ESP32 sleeps at SLEEP_DELAY_S)
```

---

## SECTION E — System State

```python
# ── State machine ─────────────────────────────────────────────────────────────
STATES = [
    'BOOT',           # Initialising peripherals
    'IDLE',           # Pump off, no fault, awaiting ON press
    'SETTING_TIMER',  # Encoder is being turned; display shows blinking timer value
    'RUNNING',        # Pump on, timer active or running indefinitely
    'DONE',           # Timer just completed; showing blinking "r 00" briefly
    'INTERRUPTED',    # Pump stopped mid-run due to power loss; remaining time preserved
    'FAULT',          # Hard fault; pump stopped; long-press OFF to acknowledge
]

# ── Mutable runtime variables ──────────────────────────────────────────────────
state              = 'BOOT'
pump_on            = False
current_hz         = 0.0

battery_soc_pct    = 100.0        # From inverter Modbus
solar_power_kw     = 0.0          # From inverter Modbus
sensor_last_read_s = 0            # Timestamp of last good inverter read
sensor_stale       = False

timer_setpoint_min  = TIMER_DEFAULT_MINUTES  # Dial-in value (encoder); 0 = no timer
timer_remaining_s   = 0                     # Countdown in seconds (during RUNNING)
timer_active        = False                 # True only when a timed run is in progress
                                            # Distinguishes "timer just expired" from
                                            # "no timer was ever set this run"
encoder_last_move_s = 0                     # Timestamp of last encoder movement

total_run_hours    = 0.0          # Persistent; saved to flash
failure_reason     = ''
fault_type         = ''
vfd_fault_count    = 0
overcurrent_s      = 0            # Seconds of sustained overcurrent
low_current_s      = 0            # Seconds of sustained low current

display_page       = 0
last_page_time_s   = 0
display_on         = True         # False when dimmed/off by inactivity timeout
done_display_start_s = 0          # Timestamp when DONE state began
prev_pump_on       = False
last_log_time_s    = 0
last_activity_s    = 0            # Resets on any button, encoder, or state change;
                                  # governs SLEEP_DELAY_S and DISPLAY_TIMEOUT_S
```

---

## SECTION F — NeoPixel Color / Pattern Definitions

Each of the 8 pixels is assigned to exactly one subsystem. This means an operator
can glance at the strip and immediately identify which part of the system needs
attention, rather than reading a single aggregate fault beacon. The freed pixel
from collapsing "pump status" from 2 to 1 is redistributed to dedicated fault channels.

```
Pixel assignment (8 pixels; longer strips can repeat or extend the pattern):

  Pixel 0 : PUMP STATUS       — is the pump running?
  Pixel 1 : POWER SOURCE      — where is the power coming from?
  Pixel 2 : BATTERY SOC       — battery charge level
  Pixel 3 : SOLAR LEVEL       — current solar generation relative to pump demand
  Pixel 4 : VFD FAULT         — VFD communication and trip faults
  Pixel 5 : SENSOR/COMMS FAULT — inverter comms, stale data, bus collision
  Pixel 6 : MECHANICAL WARNING — overcurrent, cavitation, speed clamp
  Pixel 7 : SYSTEM STATUS     — timer, RTC, storage, watchdog reset

Colors (R, G, B):
  GREEN      = (  0, 200,   0)   good / nominal
  BLUE       = (  0,   0, 200)   battery active / charging
  CYAN       = (  0, 200, 200)   solar + battery combined
  YELLOW     = (200, 150,   0)   warning / marginal condition
  ORANGE     = (200,  80,   0)   high-priority warning
  RED        = (200,   0,   0)   fault / threshold breach
  WHITE_DIM  = ( 40,  40,  40)   idle / standby
  OFF        = (  0,   0,   0)

Patterns:
  SOLID                      steady on
  SLOW_BLINK  (1 Hz)         warning — something to be aware of
  FAST_BLINK  (4 Hz)         fault — action required
  BREATHE     (sine ~0.5 Hz) relaxed idle, system is healthy
  PULSE_ONCE                 brief flash to acknowledge a refused start
```

```
FUNCTION update_neopixels(state, battery_soc_pct, solar_power_kw,
                          power_source, fault_type, timer_active, sensor_stale):

    # ── Pixel 0: Pump status ──────────────────────────────────────────────────
    IF state == 'RUNNING':          px[0] = GREEN,     SOLID
    ELIF state == 'INTERRUPTED':    px[0] = ORANGE,    SLOW_BLINK   # paused, power lost
    ELIF state == 'DONE':           px[0] = GREEN,     SLOW_BLINK   # just completed
    ELIF state == 'FAULT':          px[0] = RED,        FAST_BLINK
    ELIF state == 'SETTING_TIMER':  px[0] = WHITE_DIM,  SLOW_BLINK  # timer pending
    ELSE:                           px[0] = WHITE_DIM,  BREATHE     # idle

    # ── Pixel 1: Power source ─────────────────────────────────────────────────
    IF power_source == 'SOLAR_ONLY':   px[1] = GREEN, SOLID
    ELIF power_source == 'SOLAR_BATT': px[1] = CYAN,  SOLID
    ELIF solar_power_kw > 0:           px[1] = BLUE,  BREATHE   # solar present, pump off
    ELSE:                              px[1] = OFF               # no solar, pump off

    # ── Pixel 2: Battery SoC ─────────────────────────────────────────────────
    IF battery_soc_pct > 60:                          px[2] = GREEN,  SOLID
    ELIF battery_soc_pct > 30:                        px[2] = YELLOW, SOLID
    ELIF battery_soc_pct > BATTERY_MIN_SOC_PCT:       px[2] = ORANGE, SLOW_BLINK
    ELSE:                                             px[2] = RED,    FAST_BLINK

    # ── Pixel 3: Solar level relative to pump demand ──────────────────────────
    IF solar_power_kw >= PUMP_POWER_KW:               px[3] = GREEN,  SOLID  # covers pump
    ELIF solar_power_kw >= MIN_SOLAR_FOR_DISCHARGE:   px[3] = YELLOW, SOLID  # partial
    ELIF solar_power_kw > 0:                          px[3] = YELLOW, SLOW_BLINK  # low
    ELSE:                                             px[3] = OFF               # night / none

    # ── Pixel 4: VFD fault channel ────────────────────────────────────────────
    IF fault_type == 'FAULT_VFD_COMMS':               px[4] = RED,    FAST_BLINK
    ELIF fault_type == 'FAULT_VFD_TRIP':              px[4] = ORANGE, FAST_BLINK
    ELIF fault_type == 'FAULT_BUS_COLLISION':         px[4] = ORANGE, SLOW_BLINK
    ELSE:                                             px[4] = OFF

    # ── Pixel 5: Sensor / comms fault channel ────────────────────────────────
    IF fault_type == 'FAULT_NO_SENSOR_DATA':          px[5] = RED,    FAST_BLINK
    ELIF sensor_stale:                                px[5] = ORANGE, SLOW_BLINK
    ELSE:                                             px[5] = OFF

    # ── Pixel 6: Mechanical warning channel ───────────────────────────────────
    IF fault_type == 'FAULT_OVERCURRENT':             px[6] = RED,    FAST_BLINK
    ELIF fault_type == 'FAULT_CAVITATION':            px[6] = ORANGE, SLOW_BLINK
    ELIF current_hz == VFD_MIN_HZ AND pump_on:        px[6] = YELLOW, SOLID  # speed clamped low
    ELIF current_hz == VFD_MAX_HZ AND pump_on:        px[6] = YELLOW, SOLID  # speed clamped high
    ELSE:                                             px[6] = OFF

    # ── Pixel 7: System status ────────────────────────────────────────────────
    # Combines timer, RTC, and storage conditions; brightest / most urgent wins
    IF fault_type IN ['FAULT_RTC']:                   px[7] = ORANGE, SLOW_BLINK
    ELIF fault_type IN ['WARN_RTC_BATTERY']:          px[7] = YELLOW, SLOW_BLINK
    ELIF fault_type == 'WARN_STORAGE_RESET':          px[7] = YELLOW, SLOW_BLINK
    ELIF timer_active AND state == 'RUNNING':         px[7] = GREEN,  SOLID   # timer counting
    ELIF timer_setpoint_min != TIMER_OFF:             px[7] = CYAN,   SOLID   # timer set, pending
    ELSE:                                             px[7] = OFF             # no timer

    push_neopixels(px)
```

---

## SECTION G — 7-Segment Display Pages

The display has three operational modes. The blinking/steady distinction
communicates commitment state: blinking = value is pending, steady = actively running.

**SETTING_TIMER mode** (encoder is being turned)
  Display blinks the current setpoint: "t060" (60 min), "t 10" (10 min), "----" (no timer).
  After the encoder stops, the display continues blinking — it stays blinking as long
  as the pump is off, so the operator always knows the timer is set but not yet started.

**IDLE mode with a pending timer**
  The timer value continues to blink steadily (same appearance as SETTING_TIMER).
  This is the oven convention: blinking = "I have a value, waiting for you to start."
  The sensor-cycling pages (time, SoC, solar, Hz) are suppressed in favour of the
  pending timer. A quick press of ON commits it; a counterclockwise encoder turn
  changes it; counterclockwise past 1 min clears it back to "----".

**RUNNING mode**
  On ON press the display goes immediately steady — blinking stops the instant the
  pump starts. If a timer is active: "r 45" (45 min remaining), counting down.
  When remaining time drops below 1 minute: "r 00" → pump stops automatically.
  If no timer (----): the display reverts to cycling through sensor pages.

**IDLE mode with no timer (----)** — cycles through 4 pages every DISPLAY_CYCLE_S:
```
  Page 0 : HH.MM    Current time from RTC
  Page 1 : b XX     Battery SoC percent         (b = battery)
  Page 2 : S X.X    Solar power in kW           (S = solar)
  Page 3 : F XX     VFD frequency setpoint [Hz] (F = frequency)

  Fault override (any fault, any mode):
  Page F : F XX     Fault code (see table)
```

```
Fault codes:
  F01 = FAULT_VFD_COMMS         RS-485 to VFD unresponsive
  F02 = FAULT_VFD_TRIP          VFD reported internal fault
  F03 = FAULT_NO_SENSOR_DATA    Inverter comms lost, stale data timeout
  F04 = FAULT_OVERCURRENT       Sustained overcurrent → overtemp risk
  F05 = FAULT_CAVITATION        Sustained low current → dry-run risk
  F06 = FAULT_BUS_COLLISION     RS-485 CRC errors, address conflict
  F07 = FAULT_RTC               RTC I2C read failed
  F08 = WARN_RTC_BATTERY        RTC coin cell dead (time reset to epoch)
  F09 = WARN_STORAGE_RESET      Flash reformatted (informational)
```

---

## SECTION H — Initialisation (SETUP)

```python
FUNCTION setup():
    init_watchdog(WATCHDOG_INTERVAL_MS)
    init_gpio()                        # all pins, encoder interrupts attached
    init_uart_rs485(9600, 8N1)
    init_i2c()                         # shared by DS3231 and TM1637
    init_rtc()
    init_neopixels()
    init_filesystem()                  # LittleFS; reformat if corrupt (see B.6)
    load_persistent_state()            # total_run_hours from flash

    # Commission RS-485 bus: ping both devices
    vfd_ok      = modbus_ping(VFD_MODBUS_ADDR)
    inverter_ok = modbus_ping(INVERTER_MODBUS_ADDR)

    IF NOT vfd_ok:
        enter_fault('FAULT_VFD_COMMS')   # can't control pump; halt
        RETURN
    IF NOT inverter_ok:
        log_warn('WARN_INVERTER_COMMS')  # degraded mode; use stale values

    vfd_write_param(RAMP_TIME, VFD_RAMP_TIME_S)
    vfd_send_stop()                    # ensure pump is off at boot

    state              = 'IDLE'
    timer_setpoint_min = TIMER_DEFAULT_MINUTES
    display_splash()                   # brief "SoL" identifier on 7-seg
```

---

## SECTION I — Main Control Loop

The loop runs continuously with no blocking delays. All timing uses elapsed-
millisecond comparisons. The ESP32 enters light sleep at the bottom of the loop
when the pump is off and no input has arrived; it wakes immediately on any
button or encoder interrupt, or after SLEEP_WAKE_INTERVAL_S seconds.

```python
FUNCTION loop():
    feed_watchdog()
    now_s = millis() / 1000.0

    # ── Button reads (debounced) ──────────────────────────────────────────────
    on_pressed   = read_button_edge(SW_ON)        # True for one cycle on rising edge
    off_held_ms  = read_button_held_ms(SW_OFF)    # ms SW_OFF has been continuously held
    off_pressed  = read_button_edge(SW_OFF)       # True for one cycle on rising edge
    off_long     = (off_held_ms >= 3000)          # 3-second hold = fault clear

    IF on_pressed OR off_pressed OR off_long:
        last_activity_s = now_s
        IF NOT display_on:
            display_on = True
            set_display_brightness(NORMAL)

    # ── Encoder read (interrupt-driven accumulator) ───────────────────────────
    encoder_delta = drain_encoder_accumulator()   # signed clicks since last call
    IF encoder_delta != 0:
        encoder_last_move_s = now_s
        last_activity_s     = now_s
        IF NOT display_on:
            display_on = True
            set_display_brightness(NORMAL)
        IF state != 'RUNNING':          # encoder is ignored while pump is running
            state = 'SETTING_TIMER'
            timer_setpoint_min = encoder_step(timer_setpoint_min, encoder_delta)

    # Return to IDLE a few seconds after encoder stops (display keeps blinking)
    IF state == 'SETTING_TIMER' AND (now_s - encoder_last_move_s) > 3.0:
        state = 'IDLE'
    # Note: display continues blinking in IDLE whenever timer_setpoint_min != TIMER_OFF

    # ── OFF button: quick press = stop; long hold (≥3 s) = clear fault ─────────
    IF off_pressed AND NOT off_long:    # quick tap
        IF pump_on:
            command_pump(OFF)
            pump_on           = False
            timer_remaining_s = 0
            timer_active      = False
        # INTERRUPTED: quick tap discards remaining time and returns to idle
        IF state == 'INTERRUPTED':
            timer_remaining_s = 0
            timer_active      = False
        IF state != 'FAULT':
            state = 'IDLE'
        # FAULT stays until long-press

    IF off_long:                        # held ≥ 3 seconds: stop + clear fault
        IF pump_on:
            command_pump(OFF)
            pump_on           = False
        timer_remaining_s = 0
        timer_active      = False
        clear_fault()                   # checks that fault condition is resolved first
        state = 'IDLE'

    # ── Read power sensors (non-blocking; polls on schedule) ──────────────────
    IF elapsed(sensor_last_read_s, now_s) >= 5:     # read every 5 seconds
        result = read_inverter_modbus()
        IF result.ok:
            battery_soc_pct    = result.soc_pct
            solar_power_kw     = result.solar_kw
            sensor_last_read_s = now_s
            sensor_stale       = False
        ELSE:
            IF elapsed(sensor_last_read_s, now_s) > SENSOR_STALE_TIMEOUT_S:
                sensor_stale   = True
                # Conservatively degrade: assume battery at floor, no solar
                battery_soc_pct = BATTERY_MIN_SOC_PCT
                solar_power_kw  = 0.0
                IF pump_on:
                    command_pump(OFF)
                    pump_on = False
                    enter_fault('FAULT_NO_SENSOR_DATA')

    # ── Power sufficiency evaluation ──────────────────────────────────────────
    is_daytime       = (solar_power_kw >= MIN_SOLAR_FOR_DISCHARGE)
    solar_covers_pump = (solar_power_kw >= PUMP_POWER_KW)
    batt_above_floor  = (battery_soc_pct > BATTERY_MIN_SOC_PCT)

    # Case A: solar alone is sufficient (SoC irrelevant)
    IF solar_covers_pump:
        power_ok     = True
        power_source = 'SOLAR_ONLY'
    # Case B: solar partial, battery can supplement (daytime only)
    ELIF is_daytime AND batt_above_floor:
        power_ok     = True
        power_source = 'SOLAR_BATT'
    # Case C: insufficient power
    ELSE:
        power_ok     = False
        power_source = 'NONE'
        IF is_daytime:
            failure_reason = 'INSUFFICIENT_POWER'
        ELSE:
            failure_reason = 'NIGHT_GUARD'

    # ── ON button: start pump / resume from INTERRUPTED ──────────────────────
    IF on_pressed AND state NOT IN ['FAULT', 'RUNNING']:
        IF power_ok:
            target_hz = read_speed_setpoint()   # see subroutine I.1
            ok = command_pump(ON, target_hz)
            IF ok:
                pump_on        = True
                state          = 'RUNNING'
                failure_reason = ''
                last_activity_s = now_s
                IF state_was_interrupted:
                    # Resume: timer_remaining_s and timer_active already preserved
                    state_was_interrupted = False
                ELSE:
                    # Fresh start
                    timer_active      = (timer_setpoint_min != TIMER_OFF)
                    timer_remaining_s = timer_setpoint_min * 60
                # Display goes steady immediately (was blinking while pending/interrupted)
        ELSE:
            flash_refused_indication()   # brief NeoPixel pulse showing why

    # ── While RUNNING ─────────────────────────────────────────────────────────
    IF state == 'RUNNING':

        # Update speed if potentiometer moved beyond deadband
        target_hz = read_speed_setpoint()
        IF abs(target_hz - current_hz) > SPEED_DEADBAND_HZ:
            command_pump(ON, target_hz)   # speed update only (no restart)

        # Power still sufficient?
        IF NOT power_ok:
            command_pump(OFF)
            pump_on               = False
            last_activity_s       = now_s
            log_event('PUMP_STOPPED_POWER', failure_reason)
            IF timer_active:
                # Preserve remaining time — enter INTERRUPTED, not IDLE
                state                 = 'INTERRUPTED'
                state_was_interrupted = True
                # timer_remaining_s and timer_active intentionally kept as-is
            ELSE:
                state = 'IDLE'
            GOTO display_update

        # Check VFD current registers for fault conditions (if inverter exposes them)
        vfd_current_a = read_vfd_current()   # returns None if register unavailable
        IF vfd_current_a IS NOT None:
            IF vfd_current_a > OVERCURRENT_THRESHOLD_A:
                overcurrent_s += loop_dt_s()
                IF overcurrent_s >= OVERCURRENT_DURATION_S:
                    enter_fault('FAULT_OVERCURRENT')
            ELSE:
                overcurrent_s = 0

            IF vfd_current_a < LOW_CURRENT_THRESHOLD_A:
                low_current_s += loop_dt_s()
                IF low_current_s >= LOW_CURRENT_DURATION_S:
                    enter_fault('FAULT_CAVITATION')
            ELSE:
                low_current_s = 0

        # Countdown timer
        total_run_hours += loop_dt_s() / 3600.0
        IF timer_active:
            timer_remaining_s -= loop_dt_s()
            IF timer_remaining_s <= 0:
                # Timer expired naturally — enter DONE state for visual confirmation
                command_pump(OFF)
                pump_on              = False
                timer_active         = False
                timer_remaining_s    = 0
                state                = 'DONE'
                done_display_start_s = now_s
                last_activity_s      = now_s
                log_event('PUMP_TIMER_COMPLETE', '')
        # If timer_active is False: no timer was set; pump runs until OFF pressed.

    :display_update

    # ── Display timeout and DONE-state transition ─────────────────────────────
    # DONE → IDLE after DONE_DISPLAY_S seconds of showing blinking "r 00"
    IF state == 'DONE' AND elapsed(done_display_start_s, now_s) >= DONE_DISPLAY_S:
        state = 'IDLE'
        # timer_setpoint_min still holds the last-used value, ready for next run

    # Dim display after DISPLAY_TIMEOUT_S of no activity (pump off states only)
    IF NOT pump_on AND display_on:
        IF elapsed(last_activity_s, now_s) >= DISPLAY_TIMEOUT_S:
            display_on = False
            set_display_brightness(0)   # TM1637 supports 0–7 brightness
            set_neopixels_off()         # also dim NeoPixels

    # ── 7-segment display ─────────────────────────────────────────────────────
    SENSOR_PAGES = [
        ('HH.MM', rtc_hour_min()),
        ('b',     int(battery_soc_pct)),
        ('S',     solar_power_kw),
        ('F',     int(current_hz)),
    ]

    IF NOT display_on:
        PASS   # display is off; skip all rendering until next activity wake

    ELIF state == 'FAULT':
        # Fault code overrides everything
        show_display('F', fault_code_number(fault_type))

    ELIF state == 'DONE':
        # Timer just completed — blink "r 00" as visual confirmation
        show_display_blinking('r', 0, TIMER_BLINK_HZ)

    ELIF state == 'INTERRUPTED':
        # Power cut mid-run — blink the remaining time (paused, not counting)
        remaining_min = max(0, int(timer_remaining_s / 60))
        show_display_blinking('r', remaining_min, TIMER_BLINK_HZ)

    ELIF state == 'RUNNING' AND timer_active:
        # Timer counting: show countdown steady (committed, not blinking)
        remaining_min = max(0, int(timer_remaining_s / 60))
        show_display_steady('r', remaining_min)

    ELIF state == 'RUNNING' AND NOT timer_active:
        # No timer — cycle sensor pages while running
        IF elapsed(last_page_time_s, now_s) >= DISPLAY_CYCLE_S:
            display_page     = (display_page + 1) MOD 4
            last_page_time_s = now_s
        show_display_steady(SENSOR_PAGES[display_page])

    ELIF timer_setpoint_min != TIMER_OFF:
        # Timer pending (IDLE or SETTING_TIMER, non-zero value) — blink it
        show_display_blinking('t', timer_setpoint_min, TIMER_BLINK_HZ)

    ELIF state == 'SETTING_TIMER' AND timer_setpoint_min == TIMER_OFF:
        # Encoder at no-timer — blink the dashes
        show_display_blinking('----', None, TIMER_BLINK_HZ)

    ELSE:
        # IDLE, no timer set — cycle sensor pages
        IF elapsed(last_page_time_s, now_s) >= DISPLAY_CYCLE_S:
            display_page     = (display_page + 1) MOD 4
            last_page_time_s = now_s
        show_display_steady(SENSOR_PAGES[display_page])

    # ── NeoPixels ─────────────────────────────────────────────────────────────
    update_neopixels(state, battery_soc_pct, solar_power_kw,
                     power_source, fault_type)

    # ── Data logging ──────────────────────────────────────────────────────────
    state_changed = (pump_on != prev_pump_on)
    IF elapsed(last_log_time_s, now_s) >= LOG_INTERVAL_S OR
       (LOG_ON_STATE_CHANGE AND state_changed):
        write_log_record({
            'timestamp'        : rtc_iso(),
            'pump_on'          : pump_on,
            'power_source'     : power_source,
            'failure_reason'   : failure_reason,
            'fault_type'       : fault_type,
            'speed_hz'         : current_hz,
            'battery_soc_pct'  : battery_soc_pct,
            'solar_kw'         : solar_power_kw,
            'timer_remain_min' : int(timer_remaining_s / 60),
            'total_run_h'      : round(total_run_hours, 2),
        })
        save_persistent_state()
        last_log_time_s = now_s

    prev_pump_on = pump_on

    # ── Light sleep (pump off, inactivity threshold reached) ─────────────────
    ready_to_sleep = (
        NOT pump_on
        AND state NOT IN ['FAULT', 'SETTING_TIMER']
        AND elapsed(last_activity_s, now_s) >= SLEEP_DELAY_S
    )
    IF ready_to_sleep:
        # Display is already off by this point (DISPLAY_TIMEOUT_S < SLEEP_DELAY_S).
        # Save state to flash before sleeping so it survives a hard power cycle.
        save_persistent_state()
        # Wake sources: any button edge, encoder edge, or SLEEP_WAKE_INTERVAL_S timer.
        # On wake: all RAM (including state, timer_remaining_s, failure_reason) is intact.
        # INTERRUPTED state will show blinking remaining time and NeoPixel power
        # indicators on the very next loop iteration after wake.
        enter_light_sleep(wake_on=[SW_ON, SW_OFF, ENC_A, ENC_B],
                          max_sleep_s=SLEEP_WAKE_INTERVAL_S)
        # Execution resumes here; peripherals retained (light sleep, not deep sleep).
        last_activity_s = now_s   # reset so display wakes on next significant event
```

### I.1 — Speed Setpoint Helper

```python
FUNCTION read_speed_setpoint() -> float:
    """
    Reads potentiometer ADC, applies EMA filter, maps to Hz.
    The pot is clamped so it never commands below VFD_MIN_HZ or above VFD_MAX_HZ.
    """
    raw    = analogRead(POT_PIN)                    # 0–4095 on ESP32 12-bit ADC
    pct    = raw / 4095.0                           # 0.0 – 1.0
    # Exponential moving average to suppress noise
    pct_f  = 0.1 * pct + 0.9 * pct_prev            # alpha = 0.1
    pct_prev = pct_f
    target = VFD_MIN_HZ + pct_f * (VFD_MAX_HZ - VFD_MIN_HZ)
    RETURN clamp(target, VFD_MIN_HZ, VFD_MAX_HZ)
```

### I.2 — Stepped Encoder Timer Increment

```python
FUNCTION encoder_step(current_min: int, delta: int) -> int:
    """
    Advances the timer setpoint by one logical step per encoder click,
    using coarser steps at longer durations (logarithmic feel).

    Step table:
       1 – 10 min  →  1-minute steps
      10 – 60 min  → 10-minute steps
      60 – 1080 min → 30-minute steps

    Clockwise from TIMER_OFF  → jump to 1 min on first click
    Counterclockwise past 1 min → wrap to TIMER_OFF (no timer)
    """
    IF delta == 0:
        RETURN current_min

    sign = +1 IF delta > 0 ELSE -1
    val  = current_min

    FOR _ IN range(abs(delta)):
        IF val == TIMER_OFF:
            IF sign > 0:
                val = 1            # first click up from no-timer = 1 min
        ELIF val <= 10:
            val += sign * 1
        ELIF val <= 60:
            val += sign * 10
            # Snap to step boundary on direction change
            val = (val // 10) * 10 IF sign > 0 ELSE -(-val // 10) * 10
        ELSE:
            val += sign * 30
            val = (val // 30) * 30 IF sign > 0 ELSE -(-val // 30) * 30

        IF val < 1:
            val = TIMER_OFF        # wrap below minimum = no timer
            BREAK
        val = min(val, TIMER_MAX_MINUTES)

    RETURN val
```

---

## SECTION J — Subroutines

### J.1 — RS-485 Bus and Modbus

The MAX3485 is half-duplex. Before every transmit, assert RS485_DE_RE HIGH;
after the last byte is sent, pull it LOW and switch to receive within one
byte-time (≈ 1 ms at 9600 baud). Both the inverter and the VFD share the
same physical wire pair; only one device is queried at a time. Address
them sequentially: query inverter first, then issue VFD command.

```python
FUNCTION modbus_write_register(slave_addr, register, value) -> bool:
    RS485_DE_RE = HIGH
    frame = build_modbus_fc06(slave_addr, register, value)
    uart_write(frame)
    uart_flush()                            # wait for last byte out
    RS485_DE_RE = LOW
    response = uart_read_timeout(VFD_COMMS_TIMEOUT_MS)
    RETURN crc_valid(response) AND response.slave == slave_addr

FUNCTION modbus_read_register(slave_addr, register) -> (bool, int):
    RS485_DE_RE = HIGH
    frame = build_modbus_fc03(slave_addr, register, count=1)
    uart_write(frame)
    uart_flush()
    RS485_DE_RE = LOW
    response = uart_read_timeout(INV_COMMS_TIMEOUT_MS)
    IF crc_valid(response):
        RETURN True, response.value
    RETURN False, 0
```

### J.2 — Read Inverter Modbus

```python
FUNCTION read_inverter_modbus() -> Result(ok, soc_pct, solar_kw):
    """
    Reads battery SoC and PV input power from the hybrid inverter.
    Exact register addresses must be confirmed from your inverter's
    Modbus register map (typical values shown as placeholders).

    Common inverter registers (verify against your model):
      0x0100 — Battery SoC [%,  0–100, scale factor ×1]
      0x0101 — PV input power  [W, scale factor ×0.001 → kW]
    """
    ok1, raw_soc   = modbus_read_register(INVERTER_MODBUS_ADDR, 0x0100)
    ok2, raw_solar = modbus_read_register(INVERTER_MODBUS_ADDR, 0x0101)

    IF ok1 AND ok2:
        RETURN Result(ok=True,
                      soc_pct  = clamp(raw_soc, 0, 100),
                      solar_kw = raw_solar * 0.001)
    RETURN Result(ok=False, soc_pct=None, solar_kw=None)
```

### J.3 — Command Pump (VFD)

```python
FUNCTION command_pump(action: ON|OFF, hz: float = 0.0) -> bool:
    """
    FC-06 writes to the VFD control and frequency registers.
    Verify register addresses against your VFD Modbus manual.

    Common VFD registers (Huanyang / similar):
      0x2000 — Control word  (0x0001 = RUN forward, 0x0005 = STOP)
      0x2001 — Frequency setpoint [0.01 Hz units → send hz * 100]
    """
    IF action == ON:
        ok1 = modbus_write_register(VFD_MODBUS_ADDR, 0x2001, int(hz * 100))
        ok2 = modbus_write_register(VFD_MODBUS_ADDR, 0x2000, 0x0001)
        ok  = ok1 AND ok2
        IF ok: current_hz = hz
    ELSE:
        ok = modbus_write_register(VFD_MODBUS_ADDR, 0x2000, 0x0005)
        IF ok: current_hz = 0.0

    IF NOT ok:
        vfd_fault_count += 1
        IF vfd_fault_count >= VFD_FAULT_RETRY_COUNT:
            enter_fault('FAULT_VFD_COMMS')
    ELSE:
        vfd_fault_count = 0

    RETURN ok

FUNCTION read_vfd_current() -> float or None:
    """Read VFD output current register if the VFD exposes it."""
    ok, raw = modbus_read_register(VFD_MODBUS_ADDR, 0x3005)  # verify address
    IF ok:
        RETURN raw * 0.01   # scale depends on VFD (typically 0.01 A units)
    RETURN None
```

### J.4 — Fault Handling

```python
FUNCTION enter_fault(fault: str):
    fault_type     = fault
    failure_reason = fault
    state          = 'FAULT'
    IF pump_on:
        command_pump(OFF)   # best-effort; may fail if comms fault
        pump_on = False
    log_event('FAULT_ENTERED', fault)

FUNCTION clear_fault():
    """
    Called when OFF is pressed while in FAULT state.
    Checks that the fault condition is actually resolved before clearing.
    """
    IF fault_type == 'FAULT_VFD_COMMS' AND NOT modbus_ping(VFD_MODBUS_ADDR):
        RETURN              # VFD still unreachable; don't clear
    IF fault_type == 'FAULT_NO_SENSOR_DATA' AND sensor_stale:
        RETURN              # Inverter still silent; don't clear
    # All other faults: allow operator to clear after inspection
    fault_type     = ''
    failure_reason = ''
    vfd_fault_count = 0
    overcurrent_s   = 0
    low_current_s   = 0
    state           = 'IDLE'
    log_event('FAULT_CLEARED', '')
```

### J.5 — Persistent State

```python
FUNCTION save_persistent_state():
    write_json('/config/state.json', {
        'total_run_hours'      : total_run_hours,
        'timer_setpoint_min'   : timer_setpoint_min,
        # INTERRUPTED state fields — allows resumption after a hard power cycle
        'interrupted'          : (state == 'INTERRUPTED'),
        'timer_remaining_s'    : timer_remaining_s if state == 'INTERRUPTED' else 0,
        'timer_active'         : timer_active,
        'failure_reason'       : failure_reason,
    })

FUNCTION load_persistent_state():
    IF file_exists('/config/state.json'):
        d = read_json('/config/state.json')
        total_run_hours    = d.get('total_run_hours',    0.0)
        timer_setpoint_min = d.get('timer_setpoint_min', TIMER_DEFAULT_MINUTES)
        IF d.get('interrupted', False):
            # Restore INTERRUPTED state: remaining time and reason preserved
            state                 = 'INTERRUPTED'
            state_was_interrupted = True
            timer_remaining_s     = d.get('timer_remaining_s', 0)
            timer_active          = d.get('timer_active',       False)
            failure_reason        = d.get('failure_reason',     '')
            # NeoPixels and display will reflect this on first loop iteration
    ELSE:
        total_run_hours    = 0.0
        timer_setpoint_min = TIMER_DEFAULT_MINUTES
```

---

## SECTION K — Deep Sleep vs. Light Sleep (ESP32)

This section addresses why deep sleep is not used in this application.

**Deep sleep** powers off almost everything including the CPU cores and RAM.
Current draw drops to ~10 µA but all peripheral state (UART, I2C, GPIO
interrupts) is destroyed. On wake the chip reboots from scratch, spending
hundreds of milliseconds re-initialising the RS-485 bus, the RTC, the
NeoPixels, and loading state from flash. For a safety device that must
respond to an OFF button press immediately and must be able to confirm a
pump stop via Modbus ACK, this latency is unacceptable. Additionally,
deep sleep does not retain the timer countdown state without extra
bookkeeping in RTC RAM.

**Light sleep** retains RAM and peripheral clock trees. Current draw drops
to ~0.8–2 mA (versus ~80–240 mA active), which is negligible against the
48V/2 kWh battery system. The chip wakes instantly on a configured GPIO
edge (button press, encoder tick) or after a programmed timeout. No
re-initialisation is required. This is the correct choice for this application.

**When to enter light sleep:**
  — Pump is OFF (state == 'IDLE')
  — No encoder movement in the last 3 seconds
  — State is not 'FAULT' (fault state should keep the display active)

**Wake sources configured before sleeping:**
  — SW_ON rising edge (start request)
  — SW_OFF rising edge (stop / fault clear)
  — ENC_A or ENC_B edge (timer adjustment)
  — Internal timer after SLEEP_WAKE_INTERVAL_S (for sensor poll and log write)

**RTC behaviour during sleep:**
  The DS3231 is powered independently from the 5V PSU and continues to
  keep accurate time regardless of ESP32 sleep state. On wake, the
  controller simply reads the current time from the DS3231; no
  re-synchronisation is needed.

---

## SECTION L — Simulation ↔ Controller Mapping

| Simulation (`battery_pump_analysis.py`)     | Controller equivalent                          |
|---------------------------------------------|------------------------------------------------|
| `MIN_SOLAR_FOR_DISCHARGE_KW`                | `MIN_SOLAR_FOR_DISCHARGE` constant             |
| `BATTERY_MIN_SOC_PCT`                       | `BATTERY_MIN_SOC_PCT` constant                 |
| `MAX_HOURS_PER_DAY` / `daily_run_hours`     | `timer_setpoint_min` / `timer_remaining_s`     |
| `SCHEDULE_DAYS`, `PUMP_START_HOUR`          | Not present — operator decides when to press ON|
| `failure_reason == 'ok'`                    | `state == 'RUNNING'`                           |
| `failure_reason == 'battery_empty'`         | `failure_reason == 'INSUFFICIENT_POWER'`       |
| `failure_reason == 'night'`                 | `failure_reason == 'NIGHT_GUARD'`              |
| `failure_reason == 'no_power'`              | `failure_reason == 'INSUFFICIENT_POWER'`       |
| Case A (solar covers pump, SoC irrelevant)  | `power_source = 'SOLAR_ONLY'`                  |
| Case B (solar + battery)                    | `power_source = 'SOLAR_BATT'`                  |
| Hourly simulation tick                      | Sub-second loop; `loop_dt_s()` accumulates time|

---

## SECTION M — Open Hardware Questions

| # | Question | Impact |
|---|----------|--------|
| 1 | What are the exact Modbus register addresses for your inverter model? | Section J.2 |
| 2 | What are the exact Modbus register addresses for your VFD model? | Section J.3 |
| 3 | Does the VFD expose an output current register via Modbus? | Overcurrent / cavitation detection |
| 4 | Will an SD card be fitted, or stay on internal flash only? | Log storage capacity |
| 5 | What is VFD_MIN_HZ for your emitter spec (minimum pressure)? | Determines pot floor |

---

*End of pseudo-code Rev 0.2*
