[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)

# custom_components/multizone_thermostat
see also https://community.home-assistant.io/t/multizone-thermostat-incl-various-control-options

This is a home assistant custom component. It is a thermostat including various control options, such as: on-off, PID, weather controlled. The thermostat can be used in stand-alone mode or as zoned heating (a Select heating circuit with room climates).

Note:
This is only the required software to create a (zoned) thermostat. Especially zoned heating systems will affect the flow in your heating system vy closing and opening valves. Please check your heating system if modifications are requried to handle the flow variations, such as: pump settings, bypass valves etc.

## Installation:
1. Go to <conf-dir> default /homeassistant/.homeassistant/ (it's where your configuration.yaml is)
2. Create <conf-dir>/custom_components/ directory if it does not already exist
3. Clone this repository content into <conf-dir>/custom_components/
4. Reboot HA without any configuration using the code to install requirements
5. Set up the multizone_thermostat and have fun


## thanx to:
by borrowing and continuouing on code from:
- DB-CL https://github.com/DB-CL/home-assistant/tree/new_generic_thermostat stale pull request with detailed seperation of hvac modes
- fabian degger (PID thermostat) originator PID control in generic thermostat, https://github.com/aendle/custom_components


# Explanatory notes
Within this readme several abbreviations are used to describe the working and used methodolgy. Hereunder the most relevant are described.

## Pulse Width Modulation (PWM) 
"PWM" stands for Pulse Width Modulation. Pulse Width Modulation is a technique often used in conjunction with PID controllers to regulate the amount of power delivered to a system, such as the heating elements in each zone of your underfloor heating system.

Pulse Width Modulation (PWM) is used to adjust the amount of power delivered to an electronic device by effectively turning the heating (or cooling) on and off at a "fast" rate. The "width" of the "on" time (the pulse) is varied (modulated) to represent a specific power delivery level. When the pulse is wider (meaning the device is on for a longer period), more power is delivered to the heating element, increasing the temperature. Conversely, a narrower pulse delivers less power, reducing the temperature.

https://en.wikipedia.org/wiki/Pulse-width_modulation

## proportional–integral–derivative controller (PID)

The PID controller calculates the difference between a desired setpoint (the target temperature) and the actual temperature measured by the sensors in each zone. Based on this difference (the error), and the rate of temperature change, the PID controller adjusts the PWM signal to increase or decrease the heat output, aiming to minimize the error over time and maintain a stable temperature in each zone.

The use of PWM in your underfloor heating system allows for precise control over the temperature in each zone by adjusting the duty cycle of the electrical power to the heating elements. This method is efficient and can lead to more uniform temperature control and potentially lower energy consumption, as it adjusts the heating output to the actual need in each zone.

PID controller explained:
- https://en.wikipedia.org/wiki/PID_controller
- [https://controlguru.com/table-of-contents/](https://controlguru.com/table-of-contents/)

config examples:
slow low temperature underfloor heating:
  PID_mode:
    kp: 30
    ki: 0.005
    kd: 24000

high temperature radiator:
  PID_mode:
    kp: 80
    ki: 0.09
    kd: 5000

* underfloor heating parameter optimisation: https://www.google.nl/url?sa=t&rct=j&q=&esrc=s&source=web&cd=&cad=rja&uact=8&ved=2ahUKEwi5htyg5_buAhXeQxUIHaZHB5QQFjAAegQIBBAD&url=https%3A%2F%2Fwww.mdpi.com%2F1996-1073%2F13%2F8%2F2068%2Fhtml&usg=AOvVaw3CukGrgPjpIO2eKM619BIn

# Operation modes
The multizone thermostat can operate in two modes:
- thermostats can operate stand-alone, thus without interaction with others
- thermostats can operate under a heating-circuit Select (`heat` / `cool` / `standby` / `uncoordinated`) that schedules and balances the heat request

Per room a climate thermostat needs to be configured. A thermostat can operate by either hysteris (on-off mode) or proportional mode (weather compensation and PID mode). The PID and weather compensation can be combined or one of both can be used. Only a room operating in proportional mode can join a circuit; hysteris operation (on-off by a dT) cannot run in synchronised mode with other rooms.

When a circuit Select is included, rooms that set `master:` to that select entity_id register themselves. Option `heat` or `cool` nests those rooms and PWM-switches the plant entity. Option `standby` closes valves (anti-calc still runs). Option `uncoordinated` leaves rooms on their own PWM window with offset 0. YAML order of circuit vs rooms does not matter. If the configured Select is missing after startup, the room logs an error and runs locally.

`pwm_duration` should be a **multiple** of `control_interval`. The circuit owns the PWM window (epoch). Rooms run PID at `control_interval` inside that window for the next epoch's demand; valves are not rescheduled mid-window. If `control_interval >= pwm_duration`, there are no in-window PID ticks. Floor heating can keep 5 min PID and 15 min PWM; a slow system can set both to 15 min.

# Examples
See the examples folder for examples. 
The '\examples\multizone thermostat - explained.yaml' shows an worked-out multizone example including explanation.
The '\examples\single thermostat - on_off.yaml' shows an worked-out single operating hysteris thermostat with explanation.

# Room thermostat configuration (not for the heating-circuit Select)
This thermostat is used for satellite or stand-alone operation mode. 
The thermostat can be configured for a wide variation of hardware specifications and options:
- Operation for heating and cool are specified indiviually
- The switch can be a on-off switch or proportional (0-100) valve
- The switch can be of the type normally closed (NC) or normally opened (NO)
- For sensors with irregular update intervals such as battery operated sensors an optional uncented kalman filter is included
- Window open detection can be included
- Valve stuck prevention 
- Restore operational configuration after HA reboot

## Thermostat configuration
* platform (Required): 'multizone_thermostat'
* name (Required): Name of thermostat. The climate entity_id is derived from this name (and unique_id).
* unique_id (Optional): specify name for entity in registry else unique name is based on specified sensors and switches
* master (Optional): Full Select entity_id of the heating circuit, e.g. `select.heizung_master`. If set, this thermostat registers as a room on that circuit. Omit for stand-alone.
* room_area (Optional): Floor area of this room. Required when `master:` is set (used for nesting). The circuit sums registered room areas. Default = 0

sensors (at least one sensor needs to be specified):
* sensor (Optional): entity_id of the temperature sensor, sensor.state must be temperature (float). Not required when running in weather compensation only.
* filter_mode (Optional): unscented kalman filter can be used to smoothen the temperature sensor readings. Especially usefull in case of irregular sensor updates such as battery operated devices (for instance battery operated zigbee sensor). Default = 0 (off) (see section 'sensor filter' for more details)
* sensor_out (Optional): entity_id for a outdoor temperature sensor, sensor_out.state must be temperature (float). Only required when running weather mode. No filtering possible.

* initial_hvac_mode (Optional): Set the initial operation mode. Valid values are 'off', 'cool' or 'heat'. Default = off
* initial_preset_mode (Optional): Set the default mode. Default is normal operational mode (`none`). Built-in alternatives: `standby`, `emergency`. Custom names from `extra_presets` are also allowed and must exist under the `initial_hvac_mode` HVAC block.

* precision (Optional): specifiy setpoint precision: 0.1, 0.5 or 1
* detailed_output (Optional): include detailed control output including PID contributions and sub-control (PWM) output. To include detailed output use 'True'. Use this option limited for debugging and tuning only as it increases the database size. Default = False

checks for sensor and switch:
* sensor_stale_duration (Optional): safety routine "emergency mode" to turn switches off when sensor has not updated for a specified time period. Specify time period. Activation of emergency mode is visible via a forced climate preset state. Default is not activated. 
* passive_switch_check (Optional): Enable stuck-valve / anti-calc checks. Idle time is `passive_switch_duration` per HVAC mode. Default = False.
* passive_switch_check_time (Optional): Time of day to run the check. Default 02:00. Format HH:MM.
  - Stand-alone / uncoordinated: each thermostat flushes its own valve at this time.
  - Circuit (`heat`/`cool`/`standby`): the Select builds a queue of idle rooms and flushes them **one after another**. Rooms under a coordinated circuit do not run their own daily check; their `passive_switch_check_time` is only used without a circuit or in `uncoordinated`.
  - PWM heating of other rooms continues during a flush. The flushed valve is protected (`stuck_loop` / `anti_calc_active`) so PWM cannot close it early.

recovery of settings
* restore_from_old_state (Optional): restore certain old configuration and modes after restart. Specify 'True' to activate. (setpoints, KP,KI,PD values, modes). Default = False
* restore_parameters (Optional): specify if previous controller parameters need to be restored. Specify 'True' to activate. Default = False
* restore_integral (Optional): If PID integral needs to be restored. Avoid long restoration times. Specify 'True' to activate. Default = False

### HVAC modes: heat or cool (sub entity config)
The control is specified per hvac mode (heat, cool). At least 1 to be included.
EAch HVAC mode should include one of the control modes: on-off or proportional.

Generic HVAC mode setting:
* entity_id (Required): This can be an on-off switch or a proportional valve(input_number, etc)
* switch_mode (Optional): Specify if switch (valve) is normally closed 'NC' or normally open 'NO'. Default = 'NC'

* min_target_temp (Optional): Lower limit temperature setpoint. Default heat=14, cool=20
* max_target_temp (Optional): Upper limit temperature setpoint. Default for heat=24, cool=35
* initial_target_temp (Optional): Initial setpoint at start. Default for heat=19, cool=28
* extra_presets (Optional): A list of custom presets. Needs to be in to form of a list of name and value. Defining 'extra_presets' will make away preset available. default no preset mode available. Built-in presets `none`, `standby` and `emergency` do not need to be listed here.

* passive_switch_duration (Optional): Maximum idle time before a valve is flushed. Specify a time period. Default is not activated.
  - Room (stand-alone / uncoordinated): applies to that valve.
  - Circuit: idle threshold used when selecting which rooms to flush. The circuit's own switch (often an `input_boolean`) is **not** toggled for anti-calc.
* passive_switch_opening_time (Optional): How long to keep the valve open during a flush. Default 1 minute.
  - Room: duration of that valve's flush (also used by `stuck_prevention` on the climate).
  - Circuit: delay before starting the **next** room (`opening_time + gap`). Keep this equal to the room opening time so flushes do not overlap unless `gap` is negative.
* passive_switch_gap (Optional): Extra delay between sequential circuit flushes, added to `passive_switch_opening_time`. Default 0. May be negative (overlap / thermal actuator lag). Must not be smaller than `-passive_switch_opening_time` (that value starts all due circuits at once).


#### on-off mode (Optional) (sub of hvac mode)
The thermostat will switch on or off depending the setpoint and specified hysteris. Configured under 'on_off_mode:' 
with the data (as sub of 'on_off_mode:'):
* hysteresis_on (Required): Lower bound: temperature offset to switch on. default is 0.5
* hysteresis_off (Required): Upper bound: temperature offset to switch off. default is 0.5
* min_cycle_duration (Optional): Min duration to change switch status. If this is not specified, min_cycle_duration feature will not get activated. Specify a time period.
* control_interval (Optional): Min duration to re-run an update cycle. If this is not specified, feature will not get activated. Specify a time period.

#### proportional mode (Optional) (sub of hvac mode)
Configured under 'proportional_mode:' 
Two control modes are included to control the proportional thermostat. A single one can be specfied or combined. The control output of both are summed.
- PID controller: control by setpoint and room temperature
- Weather compensating: control by room- and outdoor temperature

The proportional controller is called periodically and specified by control_interval.
If no PWM interval is defined, it will set the state of "heater" from 0 to "PWM_scale" value as for a proportional valve. Else, when "PWM_duration" is specified it will operate in on-off mode and will switch proportionally with the PWM signal.

* control_interval (Required): interval that the PID/weather controller is updated inside a PWM window. `pwm_duration` should be a multiple of `control_interval`. Specify a time period.
* PWM_duration (Optional): Set period time for PWM signal. If it's not set, PWM is sending proportional value to switch. Specify a time period. For an on-off valve `pwm_duration` should be a multiple of `control_interval`. Default = 0 (proportional valve)
* PWM_scale (Optional): Set analog output offset to 0. Example: If it's 500 the output value can be between 0 and 500. Proportional valve might have 99 as upper max, use 99 in such case. Default = 100
* PWM_resolution (optional): Set the resolution of the PWM_scale between min and max difference. Default = 50 (50 steps between 0 and PWM_scale)
* PWM_threshold (Optional): Set the minimal difference before activating switch. To avoid very short off-on-off or on-off-on changes. Default is not acitvated
* bounded_scale_to_master(Optional): scale proporitional valves with the master's PWM. 'bounded_scale_to_master' defines the scale limit. For example: 
  - bounded scale = 3
  - prop valve PWM = 15
  - master PWM = 25
  - master PWM scale = 100 
  - valve PWM output = 15 / min( 25/100, 3)

  Default = 1 (no scaling)

##### PID controller (Optional) (sub of proportional mode)
PID controller. Configured under 'PID_mode:'
error = setpoint - room temp
output = error * Kp + sum(error * dt) * Ki − (dT/dt) * Kd

kd is per °C/s (not per control_interval). Heat: Kp, Ki, Kd positive. Cool: all three negative.

with the data (as sub):
* kp (Required): Set PID parameter, p control value.
* ki (Required): Set PID parameter, i control value.
* kd (Required): Set PID parameter, d control value.
* PWM_scale_low (Optional): Overide lower bound PWM scale for this mode. Default = 0
* PWM_scale_high (Optional): Overide upper bound PWM scale for this mode. Default = 'PWM_scale'
* window_open_tempdrop (Optional): notice temporarily opened window. Define minimum temperature drop speed below which PID is frozen to avoid integral and derative build-up. drop in Celcius per hour. Should be negative value. Default = off.

##### Weather compensating controller (Optional) (sub of proportional mode)
Weather compensation controller. Configured under 'weather_mode:'
error = setpoint - outdoor_temp
output = error * ka + kb

heat mode: ka positive, kb negative
cool mode: ka negitive, kb positive

with the data (as sub):
* ka (Required): Set PID parameter, ka control value.
* kb (Required): Set PID parameter, kb control value.
* PWM_scale_low (Optional): Overide lower bound PWM scale for this mode. Default = PWM_scale * -1
* PWM_scale_high (Optional): Overide upper bound PWM scale for this mode. Default = 'PWM_scale'

# Heating-circuit Select
The circuit is a `select` entity, not a climate. Breaking change: `climate.heizung_master` → `select.heizung_master`. Rooms set `master: select.…`.

Options:
* `heat` — coordinated heating (nesting + plant PWM)
* `cool` — coordinated cooling
* `standby` — plant out of climate service (valves closed, anti-calc still runs)
* `uncoordinated` — rooms run their own PWM window with offset 0

Attributes: `satellites`, `total_area`, PWM offset/output, `anti_calc_active` (bool). The heat request stays the YAML switch (`entity_id`), PWM'd by the circuit. The anti-calc queue is in memory only and is not restored after a Home Assistant restart.

Rooms keep their own presets (`none`, `standby`, `emergency`, or a name from `extra_presets`). Circuit option changes are not copied onto rooms. Room `standby` silences only that room.

```yaml
select:
  - platform: multizone_thermostat
    name: Heizung Master
    unique_id: heizung_master
    entity_id: input_boolean.heizanforderung_fbh
    switch_mode: NC
    supported_modes:
      - heat
    initial_option: heat
    restore_from_old_state: true
    operation_mode: balanced
    pwm_duration:
      minutes: 15
    pwm_scale: 100
    pwm_resolution: 50
    pwm_threshold: 5
    lower_load_scale: 0.15
    min_opening_for_propvalve: 0
    compensate_valve_lag:
      seconds: 30
    passive_switch_check: true
    passive_switch_check_time: "13:00"
    passive_switch_duration:
      days: 15
    passive_switch_opening_time:
      minutes: 5
```

* entity_id (Required): plant switch or heat-request helper
* switch_mode (Optional): NC or NO. Default NC
* supported_modes (Optional): `heat` and/or `cool`. `standby` and `uncoordinated` are always added. Default `[heat]`
* initial_option (Optional): Default `heat`
* operation_mode (Optional): nesting method `minimal_on`, `balanced` or `continuous`. Default `balanced`
* lower_load_scale (Optional): minimum assumed heater load. Default 0.15
* pwm_duration (Optional): PWM window. Nesting runs once per window, not as a sliding 15 min from now. Should be a multiple of each room's `control_interval`
* pwm_scale / pwm_resolution / pwm_threshold: same meaning as on a room
* min_opening_for_propvalve (Optional): minimum plant PWM when a proportional valve needs heat. Default 0
* compensate_valve_lag (Optional): delay plant opening so room valves open first. Default none
* passive_switch_*: sequential anti-calc on member rooms. The plant switch is not toggled for anti-calc

# Sensor filter (filter_mode):
An unscented kalman filter is present to smoothen the temperature readings in case of of irregular updates. This could be the case for battery operated temperature sensors such as zigbee devices. This can be usefull in case of PID controller where derivative is controlled (speed of temperature change).
The filter intesity is defined by a factor between 0 to 5 (integer).
0 = no filter
5 = max smoothing

# DEBUGGING:
debugging is possible by enabling logger in configuration with following configuration
```
logger:
  default: info
  logs:
    multizone_thermostat: debug
```    
# Services callable from HA:
Several services are included to change the active configuration of a room climate or the heating-circuit Select.
Room preset `standby` can also be set with the standard Home Assistant service `climate.set_preset_mode`. Circuit mode is set with `select.select_option`.
## set_mid_diff / pwm_threshold:
Change the 'minimal_diff' / PWM threshold before the switch is operated
## set_preset_mode:
Change the room preset (`none`, `standby`, `emergency`, or a name from `extra_presets`). Circuit `standby` is a Select option and does not overwrite room presets.
## set_pid:
Change the current kp, ki, kd values of the PID or Valve PID controller
## set_integral:
Change the PID integral value contribution
## set_ka_kb:
Change the ka and kb for the weather controller
## set_filter_mode:
change the UKF filter level for the temperature sensor
## detailed_output:
Control the attribute output for PID-, WC-contributions and control output
## stuck_prevention:
Open a room valve briefly to prevent sticking (anti-calc). On the circuit Select this starts the sequential flush. Optional `force: true` on the circuit ignores the idle timer and queues all rooms that are not currently heating.
