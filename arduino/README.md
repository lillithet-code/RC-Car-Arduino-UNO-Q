# Arduino UNO Q firmware

This folder contains firmware for an Arduino UNO that controls an RC car with a standard USB webcam stream handled by the board's Linux system, a DRV8871 motor driver, a steering servo, and four light outputs.

## Files
- `uno_q_rc_car.ino` - serial-driven control sketch for movement, steering, headlights, and camera-ready status
- `car_config.h` - per-car pin mapping and default settings

## Hardware layout
- Camera: a standard USB webcam connected to the UNO Q board, handled by the board's Linux system and forwarded to the server through the host-side pipeline
- Motor driver: DRV8871 on pins D3 and D8/D9
- Steering servo: connected to D10
- Lights: four outputs on D4, D5, D6, and D7

## Car configuration
1. Open `arduino/car_config.h`.
2. Choose the active profile by uncommenting one of the `#define` lines.
3. Update the pin values if your wiring is different.
4. Upload the sketch to each Arduino UNO.

### Built-in profiles
- `CAR_PROFILE_CAR_1` uses a default speed of 180.
- `CAR_PROFILE_CAR_2` uses a default speed of 170.

## Usage
1. Open the sketch in the Arduino IDE.
2. Select the correct board and port.
3. Upload the sketch to the Arduino UNO.
4. Send commands over serial:
   - `F` forward
   - `B` backward
   - `L` left
   - `R` right
   - `S` stop
   - `0`-`9` speed levels
   - `H` toggle headlights

## Suggested wiring
- DRV8871 PWM -> D3
- DRV8871 IN1 -> D8
- DRV8871 IN2 -> D9
- Servo signal -> D10
- Light outputs -> D4, D5, D6, D7
- Common ground between Arduino and motor driver
