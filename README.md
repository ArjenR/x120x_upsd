[![CC BY-SA 4.0][cc-by-sa-shield]][cc-by-sa]

# Readme for x120x_upsd
This python program is to provide a systemd service that can manage the various aspects of the [Geekworm X120X UPS](https://geekworm.com/collections/ups-hat/Raspberry-Pi+Raspberry-Pi-5) boards for the Raspbery Pi 5.

This program is inspired by the python examples for the X120X UPS's but extended heavily.

I wrote this because I needed something a bit more robust and flexible than what was available in the original examples.

## Functionality
- Shutdown the pi on timeout of power and/or settable minimums of battery charge or voltage.
- Charge the battery to a set maximum level (charge or voltage) so not to overcharge the battery and prolong battery life.
- Only start charging when the pi has been running for a certain time so the battery can be warmed up by the Pi itself when when it might be used in colder ( < 10 degrees Celsius) environments. This is not really precise and very dependent on the environment. Adding and monitoring a temperature sensor is a todo.
- Uses the systemd journal for logging. See it using `journalctl -xeu x120x_upsd.service`
- Writes a json status report to a tmpfs based location for ingestion into other tools.
- It is meant to run as a systemd service, but can be run directly.
- A temperature sensor attached to the lithium-cells can be used to monitor the cells to be in the correct temperature range for charging or dis-charging. Currently the Adafruit DHT22 and DHT11 are implemented. Pull requests for other types are welcome.
- Cool down the case by spinning the system fan when the batteries reach 50C.

## Install
1. Clone or download this repository.
2. Review the `x12x_ups.ini` and set according to your needs. [^1]
3. Review and understand the provided `install.sh` script as it is a good practice. 
4. Run it with `sudo sh -x ./install.sh` to install files and dependencies and enable and start the service.
5. Optionally: To stop charging quickly after power on so that the deamon can manage it, add `gpio=16=pu` to `/boot/firware/config.txt` and reboot.
6. Optionally: If using the DHT11 or DHT22 temperature sensor to monitor the lithium cell(s) add the adafruit dht package to your system and the system packages it depends on[^2]:
```
sudo apt install python3-ftdi python3-sysv-ipc python3-usb python3-typing-extensions
sudo python -m pip install --break-system-packages adafruit-circuitpython-dht
```

## Todo
- ~~Add monitoring for a temperature sensor to measure battery temperature. Need to decide which sensor 1st.~~
- An api for an applet of some sorts? Partly done hack

## License
This work is licensed under a
[Creative Commons Attribution-ShareAlike 4.0 International License][cc-by-sa].

[![CC BY-SA 4.0][cc-by-sa-image]][cc-by-sa]

[cc-by-sa]: http://creativecommons.org/licenses/by-sa/4.0/
[cc-by-sa-image]: https://licensebuttons.net/l/by-sa/4.0/88x31.png
[cc-by-sa-shield]: https://img.shields.io/badge/License-CC%20BY--SA%204.0-lightgrey.svg

[^1]: Not mentioned in the ini is the parameter `disable_self_protect`. Setting this to `on` or `True` will enable you to discharge the lithium cells to the hardware default which I think is at 2.5 Volts. Some say newer cells can handle that. The script has it hardcoded at 3.0. You can set your own mimumum voltage by enabling this parameter and setting `min_voltage`.

[^2]: If there probably is a proper way to do this where the script plus depending python packages are installed together. I still have to look into that. Suggestions or a pull request are welcome.
