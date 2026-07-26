// Shared configuration for UNO Q RC car firmware.
// Uncomment exactly one profile to choose the car wiring.
#ifndef CAR_CONFIG_H
#define CAR_CONFIG_H

// Select the profile for the car you are building.
#define CAR_PROFILE_CAR_1
// #define CAR_PROFILE_CAR_2

#ifdef CAR_PROFILE_CAR_1
#define CAR_NAME "car-1"
#define MOTOR_PWM_PIN 3
#define MOTOR_IN1_PIN 8
#define MOTOR_IN2_PIN 9
#define SERVO_STEERING_PIN 10
#define LIGHT_PIN_1 4
#define LIGHT_PIN_2 5
#define LIGHT_PIN_3 6
#define LIGHT_PIN_4 7
#define CAMERA_STREAM_ENABLED true
#define DEFAULT_SPEED_LEVEL 180
#define SERIAL_BAUD_RATE 9600
#endif

#ifdef CAR_PROFILE_CAR_2
#define CAR_NAME "car-2"
#define MOTOR_PWM_PIN 3
#define MOTOR_IN1_PIN 8
#define MOTOR_IN2_PIN 9
#define SERVO_STEERING_PIN 10
#define LIGHT_PIN_1 4
#define LIGHT_PIN_2 5
#define LIGHT_PIN_3 6
#define LIGHT_PIN_4 7
#define CAMERA_STREAM_ENABLED true
#define DEFAULT_SPEED_LEVEL 170
#define SERIAL_BAUD_RATE 9600
#endif

#ifndef CAR_NAME
#define CAR_NAME "custom-car"
#define MOTOR_PWM_PIN 3
#define MOTOR_IN1_PIN 8
#define MOTOR_IN2_PIN 9
#define SERVO_STEERING_PIN 10
#define LIGHT_PIN_1 4
#define LIGHT_PIN_2 5
#define LIGHT_PIN_3 6
#define LIGHT_PIN_4 7
#define CAMERA_STREAM_ENABLED true
#define DEFAULT_SPEED_LEVEL 180
#define SERIAL_BAUD_RATE 9600
#endif

#endif
