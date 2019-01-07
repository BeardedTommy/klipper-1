# BLTouch support
#
# Copyright (C) 2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging
import homing, probe, buttons

SIGNAL_PERIOD = 0.025600
MIN_CMD_TIME = 4 * SIGNAL_PERIOD

TEST_TIME = 5 * 60.
ENDSTOP_REST_TIME = .001
ENDSTOP_SAMPLE_TIME = .000015
ENDSTOP_SAMPLE_COUNT = 4

Commands = {
    None: 0.0, 'pin_down': 0.000700, 'touch_mode': 0.001200,
    'pin_up': 0.001500, 'self_test': 0.001800, 'reset': 0.002200,
}

# BLTouch "endstop" wrapper
class BLTouchEndstopWrapper:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.position_endstop = config.getfloat('z_offset')
        # Create a pwm object to handle the control pin
        ppins = self.printer.lookup_object('pins')
        self.mcu_pwm = ppins.setup_pin('pwm', config.get('control_pin'))
        self.mcu_pwm.setup_max_duration(0.)
        self.mcu_pwm.setup_cycle_time(SIGNAL_PERIOD)
        self.next_cmd_time = 0.
        # Create an "endstop" object to handle the sensor pin
        pin = config.get('sensor_pin')
        pin_params = ppins.lookup_pin(pin, can_invert=True, can_pullup=True)
        mcu = pin_params['chip']
        mcu.register_config_callback(self._build_config)
        self.mcu_endstop = mcu.setup_pin('endstop', pin_params)
        # Setup for sensor test
        self.next_test_time = 0.
        self.pin_up_touch_triggered = config.getboolean(
            'triggered_on_pin_up_touch_mode', True)
        self.pin_up_triggered = config.getboolean('triggered_on_pin_up', False)
        self.start_mcu_pos = []
        # Calculate pin move time
        pmt = max(config.getfloat('pin_move_time', 0.200), MIN_CMD_TIME)
        self.pin_move_time = math.ceil(pmt / SIGNAL_PERIOD) * SIGNAL_PERIOD
        # Wrappers
        self.get_mcu = self.mcu_endstop.get_mcu
        self.add_stepper = self.mcu_endstop.add_stepper
        self.get_steppers = self.mcu_endstop.get_steppers
        self.home_wait = self.mcu_endstop.home_wait
        self.query_endstop = self.mcu_endstop.query_endstop
        self.query_endstop_wait = self.mcu_endstop.query_endstop_wait
        self.TimeoutError = self.mcu_endstop.TimeoutError
        # Register BLTOUCH_DEBUG command
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command("BLTOUCH_DEBUG", self.cmd_BLTOUCH_DEBUG,
                                    desc=self.cmd_BLTOUCH_DEBUG_help)
        # Debugging code to report sensor pin state changes
        ppins.reset_pin_sharing(pin_params)
        buttons = self.printer.try_load_module(config, "buttons")
        buttons.register_buttons([pin], self.sensor_debug_callback)
    def sensor_debug_callback(self, eventtime, state):
        logging.info("bltouch sensor @ %.6f is %s", eventtime, state)
    def _build_config(self):
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for stepper in kin.get_steppers('Z'):
            stepper.add_to_endstop(self)
    def printer_state(self, state):
        if state == 'connect':
            try:
                self.sync_mcu_print_time()
                self.raise_probe()
            except homing.EndstopError as e:
                raise self.printer.config_error(str(e))
    def sync_mcu_print_time(self):
        curtime = self.printer.get_reactor().monotonic()
        est_time = self.mcu_pwm.get_mcu().estimated_print_time(curtime)
        self.next_cmd_time = max(self.next_cmd_time, est_time + MIN_CMD_TIME)
    def sync_print_time(self):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        if self.next_cmd_time > print_time:
            toolhead.dwell(self.next_cmd_time - print_time)
        else:
            self.next_cmd_time = print_time
    def send_cmd(self, cmd, duration=MIN_CMD_TIME):
        self.mcu_pwm.set_pwm(self.next_cmd_time, Commands[cmd] / SIGNAL_PERIOD)
        # Translate duration to ticks to avoid any secondary mcu clock skew
        mcu = self.mcu_pwm.get_mcu()
        cmd_clock = mcu.print_time_to_clock(self.next_cmd_time)
        cmd_clock += mcu.seconds_to_clock(max(duration, MIN_CMD_TIME))
        self.next_cmd_time = mcu.clock_to_print_time(cmd_clock)
        return self.next_cmd_time
    def verify_state(self, check_start_time, check_end_time, triggered, msg):
        # Perform endstop check to verify bltouch reports desired state
        prev_positions = [s.get_commanded_position()
                          for s in self.mcu_endstop.get_steppers()]
        self.mcu_endstop.home_start(check_start_time, ENDSTOP_SAMPLE_TIME,
                                    ENDSTOP_SAMPLE_COUNT, ENDSTOP_REST_TIME,
                                    triggered=triggered)
        try:
            self.mcu_endstop.home_wait(check_end_time)
        except self.mcu_endstop.TimeoutError as e:
            raise homing.EndstopError("BLTouch failed to %s" % (msg,))
        for s, pos in zip(self.mcu_endstop.get_steppers(), prev_positions):
            s.set_commanded_position(pos)
    def raise_probe(self):
        self.send_cmd('reset')
        check_start_time = self.send_cmd('pin_up', duration=self.pin_move_time)
        check_end_time = self.send_cmd(None)
        self.verify_state(check_start_time, check_end_time,
                          self.pin_up_triggered, "raise probe")
    def test_sensor(self):
        if not self.pin_up_touch_triggered or self.pin_up_triggered:
            # Nothing to test
            return
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        if print_time < self.next_test_time:
            self.next_test_time = print_time + TEST_TIME
            return
        # Raise the bltouch probe and test if probe is raised
        self.sync_print_time()
        check_start_time = self.send_cmd('reset', duration=self.pin_move_time)
        check_end_time = self.send_cmd('touch_mode')
        self.send_cmd(None)
        self.verify_state(check_start_time, check_end_time, True,
                          "verify sensor state")
        # Test was successful
        self.next_test_time = check_end_time + TEST_TIME
        self.sync_print_time()
    def home_prepare(self):
        self.test_sensor()
        self.sync_print_time()
        self.send_cmd('pin_down', duration=self.pin_move_time - MIN_CMD_TIME)
        self.send_cmd(None)
        self.sync_print_time()
        self.mcu_endstop.home_prepare()
        self.start_mcu_pos = [(s, s.get_mcu_position())
                              for s in self.mcu_endstop.get_steppers()]
    def home_finalize(self):
        self.sync_mcu_print_time()
        self.raise_probe()
        self.sync_print_time()
        # Verify the probe actually deployed during the attempt
        for s, mcu_pos in self.start_mcu_pos:
            if s.get_mcu_position() == mcu_pos:
                raise homing.EndstopError("BLTouch failed to deploy")
        self.mcu_endstop.home_finalize()
    def home_start(self, print_time, sample_time, sample_count, rest_time):
        rest_time = min(rest_time, ENDSTOP_REST_TIME)
        self.mcu_endstop.home_start(
            print_time, sample_time, sample_count, rest_time)
    def get_position_endstop(self):
        return self.position_endstop
    cmd_BLTOUCH_DEBUG_help = "Send a command to the bltouch for debugging"
    def cmd_BLTOUCH_DEBUG(self, params):
        cmd = self.gcode.get_str('COMMAND', params, None)
        if cmd is None or cmd not in Commands:
            self.gcode.respond_info("BLTouch commands: %s" % (
                ", ".join(sorted([c for c in Commands if c is not None]))))
            return
        msg = "Sending BLTOUCH_DEBUG COMMAND=%s" % (cmd,)
        self.gcode.respond_info(msg)
        logging.info(msg)
        self.sync_print_time()
        self.send_cmd(cmd, duration=self.pin_move_time)
        self.send_cmd(None)
        self.sync_print_time()

def load_config(config):
    blt = BLTouchEndstopWrapper(config)
    config.get_printer().add_object('probe', probe.PrinterProbe(config, blt))
    return blt
