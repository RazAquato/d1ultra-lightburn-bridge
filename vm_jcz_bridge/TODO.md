# TODO — VM JCZ Bridge

## Completed

- [x] USB gadget enumerates as BJJCZ (VID 0x9588, PID 0x9899)
- [x] Endpoint 0x88 (IN) via modified dummy_hcd
- [x] Endpoint 0x02 (OUT) via stock ep2out-bulk
- [x] USB/IP export works — Windows attaches successfully
- [x] LightBurn detects device as JCZFiber, shows "Ready"
- [x] Single command handling: GetSerialNo, GetVersion, EnableLaser, etc.
- [x] List/batch command parsing: TRAVEL, MARK, SET_POWER, SET_MARK_SPEED, etc.
- [x] Framing: TRAVEL rectangle loop received and parsed
- [x] Engrave job: Full JOB_BEGIN -> parameters -> MARK paths -> JOB_END captured
- [x] Protocol libraries standalone (jcz_protocol.py, d1ultra_protocol.py)

## In Progress

### JCZ-to-D1 Ultra Translation (the actual laser driving)

The bridge currently captures JCZ commands but doesn't send them to the D1 Ultra.
The `d1ultra_protocol.py` library has the full D1 Ultra API. The translation needs:

- [ ] **Connect to laser**: Use `laser_monitor.py` to detect RNDIS interface, connect via TCP
- [ ] **Job translation**: Convert accumulated JCZ MARK/TRAVEL paths to D1 Ultra PATH_DATA
  - JCZ galvo coords (0-65535) -> mm centered on bounding box midpoint
  - JCZ SET_POWER (0-4095) -> D1 Ultra power (0.0-1.0 float64)
  - JCZ SET_MARK_SPEED -> D1 Ultra speed (mm/min float64) — needs calibration
  - JCZ SET_Q_PERIOD -> D1 Ultra frequency (kHz float64)
- [ ] **Job execution sequence**: 
  1. DEVICE_INFO (0x0018)
  2. PRE_JOB (0x0005)
  3. QUERY_14 (0x0014, sub=0x02)
  4. JOB_UPLOAD (0x0002) — job name + PNG thumbnail
  5. WORKSPACE (0x0009) — bounding box
  6. JOB_SETTINGS (0x0000) + PATH_DATA (0x0001) for each path
  7. JOB_CONTROL (0x0003) — trigger execution
  8. JOB_FINISH (0x0004)
- [ ] **Live framing**: JCZ TRAVEL-only sequences -> D1 Ultra WORKSPACE preview
- [ ] **Status feedback**: Report BUSY while D1 Ultra job is executing, READY when done
- [ ] **GetVersion polling**: LightBurn polls GetVersion rapidly during job — respond with BUSY status

### Speed/Power Calibration

- [ ] **SET_MARK_SPEED scaling**: JCZ value -> mm/min for D1 Ultra
  - LightBurn sent 0x076f (1903) for 5000mm/s setting
  - Current formula: `p1 * 60 / 256` = 446 mm/min — likely wrong
  - Need to test with known speeds and measure actual output
  - Real BJJCZ boards may use different scaling per firmware
- [ ] **SET_POWER scaling**: JCZ 0-4095 -> D1 Ultra 0.0-1.0
  - LightBurn sent 0x0B33 (2867) for 70% — that's 2867/4095 = 0.700 (correct!)
  - This mapping appears correct, but needs hardware verification
- [ ] **SET_Q_PERIOD**: JCZ period (us) -> D1 Ultra frequency (kHz)
  - LightBurn sent 0x03E8 (1000us) -> 1.0 kHz
  - D1 Ultra may not support Q-switching — may need to ignore or map differently

## Future Work

### Reliability
- [ ] Handle LightBurn disconnect/reconnect gracefully
- [ ] Handle USB/IP disconnect/reconnect without bridge restart
- [ ] Watchdog for bridge process
- [ ] Persistent systemd service with auto-restart

### Features
- [ ] Fill/raster engraving support (JCZ has raster commands we don't parse yet)
- [ ] Multiple passes (JCZ SET_PASSES -> D1 Ultra passes parameter)
- [ ] Conveyor belt mode (D1 Ultra supports 220x800mm with conveyor attachment)
- [ ] Cancel job (LightBurn sends Reset command -> D1 Ultra JOB_CONTROL cancel)

### Infrastructure
- [ ] Replace stock dummy_hcd in /lib/modules permanently (avoid rmmod on every boot)
- [ ] DKMS package for the modified dummy_hcd (survives kernel upgrades)
- [ ] Debian package for the whole bridge
- [ ] Auto-attach script for Windows (scheduled task or startup bat)

### Testing
- [ ] End-to-end test with real D1 Ultra hardware
- [ ] Measure actual engrave accuracy (galvo coord calibration)
- [ ] Test with different LightBurn versions
- [ ] Test with EzCad2 (another JCZ client)
- [ ] Stress test: large jobs, many paths, rapid framing cycles
