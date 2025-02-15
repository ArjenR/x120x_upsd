#!/usr/bin/env python3

"""
This program is inspired by the python examples for the X120X UPS's
and some examples in python manuals, some blogs, and code examples found on the internet.

It manages the X120X ups board for the Raspberry Pi and runs as a UPS daemon.
It manages charging of the lithium cells.
It shuts down the pi when condfigured parameters are reached.
"""


import configparser
import os
import signal
import smbus2
import systemd.daemon
import struct
import sys
import time
import traceback
import json

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from gpiozero import InputDevice, Button
from subprocess import call
from threading import Thread, Event, Timer

# Configuratiopn
config = configparser.ConfigParser()
config['DEFAULT'] = {
    'max_voltage': '0',
    'max_charge_capacity': '80',
    'min_charge_capacity': '20', 
    'battery_report_schedule': '0 * * * *',
    'ac_max_downtime': '5',
    'warmup_time': '0',
    'pid_file': '',
    'json_report_file': '',
    'json_report_period': '',
    'disable_self_protect': 'Off',
    'no_power_at_start': 'default'
}

CONFIG_FILE = '/usr/local/etc/x120x_upsd.ini'

# Constants
CHG_ONOFF_PIN = 16
CHG_PRESENT_PIN = 6
BUS_ADDRESS = 1
BATTERY_ADDRESS = 0x36


class TimerError(Exception):
    """A custom exception used to report errors in use of Timer class"""

class Timer:
    def __init__(self):
        self._start_time = None
    
    def elapsed_time(self):
        """Return elapsed time"""
        if self._start_time is None:
            return 0
        return time.perf_counter() - self._start_time

    def start(self):
        """Start a new timer"""
        if self._start_time is not None:
            raise TimerError(f'Timer is running. Use .stop() to stop it')

        self._start_time = time.perf_counter()

    def stop(self):
        """Stop the timer, and report the elapsed time"""
        if self._start_time is None:
            raise TimerError(f'Timer is not running. Use .start() to start it')
        elapsed = self.elapsed_time()
        self._start_time = None
        return(elapsed)
    
    def active(self):
        """Is the timer running."""
        return not(self._start_time is None)


class Charger:
    def __init__(self, charger_control_pin, charger_pin):
        self._charger_control_pin = charger_control_pin
        self._charger_button = Button(charger_pin)
        self._charging = None

    def start(self):
        InputDevice(self._charger_control_pin, pull_up=False)
        self._charging = True

    def stop(self):
        InputDevice(self._charger_control_pin, pull_up=True)
        self._charging = False

    @property
    def present(self):
        return not self._charger_button.is_pressed

    @property
    def charging(self):
        return self._charging


class Battery:
    def __init__(self, bus_address, address, charger, max_voltage=0, min_voltage=0, max_capacity=None, min_capacity=20,
                warmup_time=60, disable_self_protect=False, stopsignal=None, json_report_file=''):
        self._bus = smbus2.SMBus(bus_address)
        self._address = address
        self._charger = charger
        self._max_capacity = max_capacity
        self._recharge_hysteresis = 3 # percentage at which battery may slowely lose charge before recharging
        self._protect_voltage = 3.0
        self._max_voltage = max_voltage if (max_voltage <= 4.2 and max_voltage >= 3.5) else 0
        self._min_capacity = min_capacity if (min_capacity >= 20 and min_capacity <= 80) else 10
        self._min_voltage = min_voltage if min_voltage <= 4 else 0
        self._warmup_time = warmup_time
        self._warmup_thread = None
        self._charge_control_thread = None
        self._regular_report = None
        self._stopsignal = stopsignal
        self._json_report_file = json_report_file
        self.disable_self_protect=disable_self_protect
        self._charger.stop()
        if not self.disable_self_protect: self.start_selfprotect()
        
        
    @property
    def current_voltage(self):
        read = self._bus.read_word_data(self._address, 2) # reads word data (16 bit)
        swapped = struct.unpack('<H', struct.pack('>H', read))[0] # big endian to little endian
        voltage = swapped * 1.25 / 1000 / 16 # convert to understandable voltagesystemd.daemon.notify('READY=1')
        return voltage
    
    @property
    def current_capacity(self):
        read = self._bus.read_word_data(self._address, 4) # reads word data (16 bit)
        swapped = struct.unpack('<H', struct.pack('>H', read))[0] # big endian to little endian
        capacity = swapped / 256 # convert to 1-100% scale
        return capacity
    
    @property
    def max_voltage(self):
        return self._max_voltage
    
    @property
    def max_capacity(self):
        return self._max_capacity
    
    @property
    def min_voltage(self):
        return self._min_voltage
    
    @property
    def min_capacity(self):
        return self._min_capacity
    
    def json_report(self):
        return {
                    'current_capacity': self.current_capacity,
                    'current_voltage': self.currtent_voltage,
                    'min_capacity': self.min_capacity,
                    'min_voltage': self.min_voltage,
                    'max_capacity': self.max_capacity,
                    'max_voltage': self.max_voltage,
                    'charger_present': self._charger.present,
                    'charger_charging': self._charger.charging & self._charger.present
                }

    def _minutes_since_boot(self):
        return time.clock_gettime(time.CLOCK_BOOTTIME) / 60

    @property
    def is_warmed_up(self):
        return self._minutes_since_boot() > self._warmup_time
    
    def needs_charging(self):
        if self._max_capacity != None and self._max_capacity >= 20 \
            and self._max_capacity <= 100 and self._max_capacity <= self.current_capacity:
            return False
        elif self._max_capacity != None and self._max_capacity >= 20 \
                and self._max_capacity <= 100 \
                and self.current_capacity < self._max_capacity:
            return True
        elif self.current_voltage >= self._max_voltage:
            return False
        elif self.current_voltage < (self._max_voltage - (self._max_voltage * self._recharge_hysteresis / 100)):
            return True
        return None
  
    def battery_report(self):
        return (f'Battery is currently at {self.current_capacity:0.0f}%, {self.current_voltage:0.2f}V ' \
                f'and {"not " if not self._charger.charging & self._charger.present else ""}charging. ' \
                f'It {"needs" if self.needs_charging() else "does not need"} charging. ' \
                f'Charger is {"not " if not self._charger.present else ""}present.')
    
    def start_charge_control(self):
        if self._charge_control_thread and self._charge_control_thread.is_alive():
            return
        print(f'Starting charge control.', flush=True)
        self._stop_charge_control = Event()
        self._charge_control_thread = Thread(target=self._charge_control, daemon=True)
        self._charge_control_thread.start()

    def stop_charge_control(self):
        if self._charge_control_thread and self._charge_control_thread.is_alive():
            self._stop_charge_control.set()
            self._charge_control_thread.join()

    def start_warmup(self):
        if self._warmup_thread and self._warmup_thread.is_alive():
            return
        self._stop_warmup = Event()
        self._warmup_thread = Thread(target=self._wait_for_warmup, daemon=True)
        self._warmup_thread.start()

    def stop_warmup(self):
        if self._warmup_thread and self._warmup_thread.is_alive():
            self._stop_warmup.set()
            self._warmup_thread.join()

    def _wait_for_warmup(self):
        if not self.is_warmed_up:
            print(f'Waiting for the computer to warm the batteries for {self._warmup_time - self._minutes_since_boot() } minutes.', flush=True)
            while not self.is_warmed_up or not (self._stopsignal != None and self._stopsignal.kill_now):
                if not self._charger.present:
                    print('Battery is not warmed up yet and no charger present!', flush=True)
                    call('sudo nohup shutdown -h now', shell=True)
                time.sleep(10)
        if not (self._stopsignal != None and self._stopsignal.kill_now):
            print('Batteries are warmed up. Starting charging control process', flush=True)
            self.start_charge_control()

    def _charge_control(self):
        while not self._stop_charge_control.is_set() or not (self._stopsignal != None and self._stopsignal.kill_now):
            if self.needs_charging() == False and self._charger.charging:
                self._charger.stop()
                print(f'Charging stopped at {self.current_capacity:0.0f}%, {self.current_voltage:0.2f}V.', flush=True)
            elif self.needs_charging() == True and not self._charger.charging:
                self._charger.start()
                print(f'Charging {"started" if self._charger.present else "needed"} at {self.current_capacity:0.0f}%, {self.current_voltage:0.2f}V.', flush=True)
            time.sleep(30)
    
    def start_selfprotect(self):
        self._selfprotect_thread = Thread(target=self._selfprotect, daemon=True)
        self._selfprotect_thread.start()

    def _selfprotect(self):
        while (self.current_voltage >= self._protect_voltage) or not (self._stopsignal != None and self._stopsignal.kill_now):
            time.sleep(30)
        # If this loop exits, power is too low!
        print('Battery is too low! Emergency shutdown!', flush=True)
        call('sudo nohup shutdown -h now', shell=True)


class UPS_monitor:
    def __init__(self, charger, battery, max_duration=0, stopsignal=None):
        self._charger = charger
        self.battery = battery
        self._max_duration = max_duration * 60
        self._timer_no_power = Timer()
        self._shutdown_initiated = False
        self._msg_no_power_no_charging_sent = False
        self._monitor_battery_thread = None
        self._monitor_charger_thread = None
        self._stopsignal = stopsignal
     
    def initiate_5_minute_shutdown(self, message):
        if not self._shutdown_initiated:
            print(f'Initiating shutdown. {message}', flush=True)
            call('sudo shutdown -P +5 "Power failure, shutdown in 5 minutes."', shell=True)
        self._shutdown_initiated = True
    
    def initiate_emergency_shutdown(self, message):
        print(f'Initiating shutdown. {message}', flush=True)
        call('sudo nohup shutdown -h now', shell=True)
        self._shutdown_initiated = True

    def cancel_shutdown(self):
        print(f'Cancelling shutdown.', flush=True)
        call('sudo shutdown -c "Shutdown is cancelled"', shell=True)
        self._shutdown_initiated = False
        
    def start_monitor_processes(self):
        if not(self._monitor_battery_thread and self._monitor_battery_thread.is_alive()):
            self._stop_monitor_battery = Event()
            self._monitor_battery_thread = Thread(target=self._monitor_battery, daemon=True)
            self._monitor_battery_thread.start()
        if self._max_duration and not(self._monitor_charger_thread and self._monitor_charger_thread.is_alive()):
            self._stop_monitor_charger = Event()
            self._monitor_charger_thread = Thread(target=self._monitor_charger, daemon=True)
            self._monitor_charger_thread.start()

    def stop_monitor_processes(self):
        if self._monitor_battery_thread and self._monitor_battery_thread.is_alive():
            self._stop_monitor_battery.set()
            self._monitor_battery_thread.join()
        if self._monitor_charger_thread and self._monitor_charger_thread.is_alive():
            self._stop_monitor_charger.set()
            self._monitor_charger_thread.join()
    
    def _monitor_battery(self):
        while not self._stop_monitor_battery.is_set():
            c = self.battery.current_capacity
            v = self.battery.current_voltage
            c_min = self.battery.min_capacity
            v_min = self.battery.min_voltage
            if not self._charger.present:
                if c <= c_min and not self._shutdown_initiated:
                    self.initiate_5_minute_shutdown(f'Capacity {c}% below setpoint {c_min}%')
                elif v_min != None and v <= v_min and not self._shutdown_initiated:
                    self.initiate_5_minute_shutdown(f'Voltage {v:0.2f}V below setpoint {v_min:0.2f}V')
            time.sleep(10)

    def _monitor_charger(self):
        while not self._stop_monitor_charger.is_set() or not (self._stopsignal != None and self._stopsignal.kill_now):
            if not self._msg_no_power_no_charging_sent and not self._charger.present \
                    and self._timer_no_power.elapsed_time() == 0 and not self.battery.needs_charging():
                print('Power failed, but the battery does not need charging', flush=True)
                self._msg_no_power_no_charging_sent = True
            elif not self._charger.present and self._timer_no_power.elapsed_time() == 0 and self.battery.needs_charging():
                self._timer_no_power.start()
                print('Power failed.', flush=True)
            elif self._charger.present and self._timer_no_power.elapsed_time() != 0:
                if self._shutdown_initiated:
                    self.cancel_shutdown()
                print(f'Power returned after {self._timer_no_power.stop():0.0f} seconds', flush=True)
                self._msg_no_power_no_charging_sent = False
            elif not self._shutdown_initiated and self._timer_no_power.elapsed_time() >= self._max_duration:
                self.initiate_5_minute_shutdown(f'Power failed for {(self._timer_no_power.elapsed_time()/60):0.0f} minutes')
            time.sleep(30)


class Publisher:
    '''This class will handle various external communication whith the UPS daemon'''
    def __init__(self, battery, stop_signal = None, battery_report_schedule='', json_report_file='', json_report_period=10):
        self._battery = battery
        self._stop_signal = stop_signal
        self._battery_report_schedule = battery_report_schedule
        self._json_report_file = json_report_file
        self._json_report_period = json_report_period

    def publish_json_file(self):
        if self._json_report_file != '':
            try:
                with open(self._json_report_file, 'w') as json_file:
                    json.dump(self._battery.json_report(), json_file)
            except IOError as e:
                print(f"Error writing battery report to JSON file ({self._json_report_file}): {e}", flush=True)

    def _publish_json_file_thread(self):
        while not self._stop_publish_json_file_thread.is_set():
            self.publish_json_file()
            time.sleep(self._json_report_period)

    def start_publish_json_file_thread(self):
        if self._publish_json_file_thread and self._publish_json_file_thread.is_alive():
            return
        self._stop_publish_json_file_thread = Event()
        self._publish_json_file_thread = Thread(target=self._publish_json_file_thread, daemon=True)
        self._publish_json_file_thread.start()
    
    def stop_publish_json_file_thread(self):
        if self._publish_json_file_thread and self._publish_json_file_thread.is_alive():
            self._stop_publish_json_file_thread.set()
            self._publish_json_file_thread.join()

    def print_battery_report(self):
        print(self._battery.battery_report(), flush=True)
    
    def start_regular_battery_report(self, schedule):
        self._regular_report = BackgroundScheduler()
        self._regular_report.add_job(self.print_battery_report, CronTrigger.from_crontab(schedule))
        self._regular_report.start()

    def stop_regular_battery_report(self):
        if self._regular_report:
            self._regular_report.stop()
            self._regular_report = None

    def _start_publishers(self):
        if self._json_report_file != '':
            self.start_publish_json_file_thread()
        if self._battery_report_schedule != '':
            self.start_regular_battery_report()

    def _stop_publishers(self):
        if self._json_report_file != '':
            self.stop_publish_json_file_thread()
        if self._battery_report_schedule != '':
            self.stop_regular_battery_report()

class GracefullKiller:
    kill_now = False
    def __init__(self):
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGHUP, self.signal_handler)

    def signal_handler(self, sig, frame):
        self.kill_now = True
        systemd.daemon.notify('STOPPING=1')
        print(f'Signal {sig} received. Shutting down.', flush=True)
        if PIDFILE != '' and os.path.isfile(PIDFILE):
            os.unlink(PIDFILE)
        sys.exit(0)

if __name__ == '__main__':
    print('Starting up UPS control daemon.', flush=True)
    
    config.read(CONFIG_FILE)
    MAX_VOLTAGE             = config['general'].getfloat('max_voltage')
    MIN_VOLTAGE             = config['general'].getfloat('min_voltage')
    MAX_CHARGE_CAPACITY     = config['general'].getint('max_charge_capacity')
    MIN_CHARGE_CAPACITY     = config['general'].getint('min_charge_capacity')
    AC_MAX_DOWNTIME         = config['general'].getint('ac_max_downtime')
    WARMUP_TIME             = config['general'].getint('warmup_time')
    BATTERY_REPORT_SCHEDULE = config['general'].get('battery_report_schedule')
    PIDFILE                 = config['general'].get('pid_file')
    DISABLE_SELF_PROTECT    = config['general'].getboolean('disable_self_protect')
    NO_POWER_AT_START       = config['general'].get('no_power_at_start')
    JSON_REPORT_FILE        = config['general'].get('json_report_file').strip().strip('"')
    JSON_REPORT_PERIOD      = config['general'].getint('json_report_period')

    # Ensure only one instance of the script is running
    if PIDFILE != '':
        pid = str(os.getpid())
        if os.path.isfile(PIDFILE):
            print('Script already running.', flush=True)
            exit(1)
        else:
            with open(PIDFILE, 'w') as f:
                f.write(pid)
  
    try:
        stopsignal = GracefullKiller()
        charger = Charger(CHG_ONOFF_PIN, CHG_PRESENT_PIN)
        battery = Battery(BUS_ADDRESS, BATTERY_ADDRESS, charger, max_voltage=MAX_VOLTAGE, \
                          min_voltage=MIN_VOLTAGE, max_capacity=MAX_CHARGE_CAPACITY, \
                          min_capacity=MIN_CHARGE_CAPACITY, warmup_time=WARMUP_TIME, \
                          disable_self_protect=DISABLE_SELF_PROTECT, \
                          stopsignal=stopsignal)
        publisher = Publisher(stop_signal=stopsignal, battery=battery, battery_report_schedule=BATTERY_REPORT_SCHEDULE,
                              json_report_file=JSON_REPORT_FILE, json_report_period=JSON_REPORT_PERIOD)
        battery.print_battery_report()
        if (NO_POWER_AT_START not in ['run_till_minimums', 'run_till_protect'] and not charger.present) or charger.present:
            # failsafe, anything other is handled as default.
            if NO_POWER_AT_START not in ['run_till_minimums', 'run_till_protect', 'standard']:
                raise Warning(f'Warning: no_power_at_start value \"{NO_POWER_AT_START}\" is not implemented. Using "standard" as fall-back.')
            battery.start_warmup() # start_warmup will start the other battery threads once done.
            ups = UPS_monitor(charger, battery, max_duration=AC_MAX_DOWNTIME, stopsignal=stopsignal) 
            ups.start_monitor_processes()
        elif not charger.present and NO_POWER_AT_START == 'run_till_minimums':
            battery.start_charge_control() # Do not warmup, handle charging if power return
            ups = UPS_monitor(charger, battery, max_duration=0, stopsignal=stopsignal) # only shutdown at minimum.
        elif not charger.present and NO_POWER_AT_START == 'run_till_protect':
            battery.start_charge_control() # Do not warmup, handle charging if power returns
            # We are not starting ups for this session.
            
        systemd.daemon.notify('READY=1')
        print('Startup complete.', flush=True)
        while True:
            time.sleep(60)
            systemd.daemon.notify('WATCHDOG=1')

    except Exception as e:
        print(f'There was an error: {e}', flush=True)
        traceback.print_exc()
        sys.exit(1)

    finally:
        if PIDFILE != '' and os.path.isfile(PIDFILE):
            os.unlink(PIDFILE)
        sys.exit(0)

