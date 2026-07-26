// Arduino UNO Q RC Car firmware for camera streaming and remote control.
// This version is designed for a DRV8871 motor driver, a steering servo,
// and four lighting outputs controlled from the UNO.
//
// Edit arduino/car_config.h to choose the active car profile.
// Each profile can use a different pin map and default speed.
//
// Serial commands:
//   F = forward
//   B = backward
//   L = left
//   R = right
//   S = stop
//   0-9 = speed (0-9)
//   H = headlights on/off

#include <Servo.h>
#include "car_config.h"

#if __has_include("Arduino_RouterBridge.h")
#include "Arduino_RouterBridge.h"
#define HAS_ROUTER_BRIDGE 1
#else
#define HAS_ROUTER_BRIDGE 0
#endif

Servo steeringServo;

const int motorPWM = MOTOR_PWM_PIN;
const int motorIN1 = MOTOR_IN1_PIN;
const int motorIN2 = MOTOR_IN2_PIN;
const int steeringPin = SERVO_STEERING_PIN;
const int lightPins[] = {LIGHT_PIN_1, LIGHT_PIN_2, LIGHT_PIN_3, LIGHT_PIN_4};

int speedLevel = DEFAULT_SPEED_LEVEL;
bool headlightsOn = false;
int steeringAngle = 90;

void setup() {
  pinMode(motorPWM, OUTPUT);
  pinMode(motorIN1, OUTPUT);
  pinMode(motorIN2, OUTPUT);

  for (int i = 0; i < 4; i++) {
    pinMode(lightPins[i], OUTPUT);
    digitalWrite(lightPins[i], LOW);
  }

  steeringServo.attach(steeringPin);
  steeringServo.write(steeringAngle);

  Serial.begin(SERIAL_BAUD_RATE);
  Serial.print("Car profile: ");
  Serial.println(CAR_NAME);
  Serial.println("Camera stream enabled");

#if HAS_ROUTER_BRIDGE
  Bridge.begin();
  Bridge.provide_safe("car_forward", rpc_forward);
  Bridge.provide_safe("car_back", rpc_back);
  Bridge.provide_safe("car_left", rpc_left);
  Bridge.provide_safe("car_right", rpc_right);
  Bridge.provide_safe("car_stop", rpc_stop);
  Bridge.provide_safe("car_lights_on", rpc_lights_on);
  Bridge.provide_safe("car_lights_off", rpc_lights_off);
  Serial.println("Router Bridge command handlers ready");
#endif

  stopCar();
}

void loop() {
  if (Serial.available() > 0) {
    char command = toupper(Serial.read());

    switch (command) {
      case 'F':
        driveForward();
        break;
      case 'B':
        driveBackward();
        break;
      case 'L':
        turnLeft();
        break;
      case 'R':
        turnRight();
        break;
      case 'S':
        stopCar();
        break;
      case 'H':
        toggleHeadlights();
        break;
      case '0':
      case '1':
      case '2':
      case '3':
      case '4':
      case '5':
      case '6':
      case '7':
      case '8':
      case '9':
        speedLevel = 40 + (command - '0') * 20;
        Serial.print("Speed:");
        Serial.println(speedLevel);
        break;
      default:
        break;
    }
  }
}

void driveForward() {
  analogWrite(motorPWM, speedLevel);
  digitalWrite(motorIN1, HIGH);
  digitalWrite(motorIN2, LOW);
  steeringServo.write(steeringAngle);
}

void driveBackward() {
  analogWrite(motorPWM, speedLevel);
  digitalWrite(motorIN1, LOW);
  digitalWrite(motorIN2, HIGH);
  steeringServo.write(steeringAngle);
}

void turnLeft() {
  analogWrite(motorPWM, speedLevel / 2);
  digitalWrite(motorIN1, HIGH);
  digitalWrite(motorIN2, LOW);
  steeringAngle = 60;
  steeringServo.write(steeringAngle);
}

void turnRight() {
  analogWrite(motorPWM, speedLevel / 2);
  digitalWrite(motorIN1, HIGH);
  digitalWrite(motorIN2, LOW);
  steeringAngle = 120;
  steeringServo.write(steeringAngle);
}

void stopCar() {
  analogWrite(motorPWM, 0);
  digitalWrite(motorIN1, LOW);
  digitalWrite(motorIN2, LOW);
  steeringAngle = 90;
  steeringServo.write(steeringAngle);
}

void toggleHeadlights() {
  headlightsOn = !headlightsOn;
  for (int i = 0; i < 4; i++) {
    digitalWrite(lightPins[i], headlightsOn ? HIGH : LOW);
  }
}

#if HAS_ROUTER_BRIDGE
void rpc_forward() {
  driveForward();
}

void rpc_back() {
  driveBackward();
}

void rpc_left() {
  turnLeft();
}

void rpc_right() {
  turnRight();
}

void rpc_stop() {
  stopCar();
}

void rpc_lights_on() {
  headlightsOn = false;
  toggleHeadlights();
}

void rpc_lights_off() {
  headlightsOn = true;
  toggleHeadlights();
}
#endif
