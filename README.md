# Radar Firmware (Based on LibreVNA hardware)

### 0️⃣ Boot without USB-C adapter PSU
This configuration change is necessary because the Raspberry Pi 5 has a higher power requirement, and setting PSU_MAX_CURRENT=5000 ensures that the Pi recognizes that the connected power supply can deliver up to 5A. This can help resolve issues where the Pi fails to boot properly due to insufficient power, especially when using power-hungry USB devices or when booting from USB. Do the following:
- `sudo rpi-eeprom-config --edit`
- Add the following line --> `PSU_MAX_CURRENT=5000`

### 1️⃣ Adjust time to UTC and enable RTC
- `sudo raspi-config`
- `5 Localisation Options` --> `L2 Timezone` --> `None of the above` --> `UTC`
- Reboot Pi `sudo reboot`

Enable RTC
- `sudo raspi-config`
- `3 Interface Options` → `I5 I2C` --> `Yes` --> `Ok`
- Reboot Pi `sudo reboot`
- Install i2cdetect and check if address 0x68 (DS3231) is available
  ```
  sudo apt update
  sudo apt install i2c-tools
  i2cdetect -y 1
  ```
- Change file `config.txt`
  ```
  sudo nano /boot/firmware/config.txt
  ```
- Add following lines to [ALL]
  ```
  dtparam=rtc=off
  dtoverlay=i2c-rtc,ds3231
  ```
- Reboot Pi `sudo reboot`
- Check system data --> `date`
- If ok, write data --> `sudo hwclock -w`
- Check RTC data --> `sudo hwclock -r`

### 2️⃣ Create SSH Key to clone this repository
- On the raspberry pi
	```
	ssh-keygen -t ed25519 -C "jarne.vanmulders@kuleuven.be"
	cat ~/.ssh/id_ed25519.pub
	```
- ENTER 3x
- Add key to github account [online] --> settings --> SSH Key
- Clone repo via SSH

### 3️⃣ Get custom firmware ready
1. Install git
	```
	sudo apt update
	sudo apt install git -y
	```
2. Git clone via SSH KEY on rpi in home dir: `git clone git@github.com:wavetrax-by-dramco/Radar-LibreVNA-Firmware.git`
3. Download https://github.com/jankae/LibreVNA/releases the RPI release (✅ Latest version that works: 1.6.2) and place it in the home dir of the user or download it from the latest build: https://github.com/jankae/LibreVNA/actions/workflows/Build.yml
	```
 	wget https://github.com/jankae/LibreVNA/releases/download/v1.6.2/LibreVNA-GUI-RPi5-v1.6.2.zip
	unzip LibreVNA-GUI-RPi5-v1.6.2.zip
 	rm LibreVNA-GUI-RPi5-v1.6.2.zip
	```
4. Make the LibreVNA software executable: `chmod +x LibreVNA-GUI`
5. Install QT6 with `sudo apt-get install qt6-base-dev libqt6svg6` (with all dependencies)
6. You can test it with `sudo ./LibreVNA-GUI --port 1234 --no-gui -platform vnc` (won't work just yet, but software should start)
7. Move config.yaml to home directory
	```
 	mv config.yaml ..
	``` 

### 4️⃣ LibreVNA 
1. PART I: Preperations
	1. Plug in LibreVNA and check if it gets recognized: `lsusb` should read "Generic VNA" somewhere. 
	2. Adjust USB permissions for LibreVNA. Create and edit file: `sudo nano /etc/udev/rules.d/51-vna.rules`:
		``` 
		SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="564e", MODE:="0666"
		SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="4121", MODE:="0666"
		SUBSYSTEMS=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="4121", MODE:="0666"
		```
	3. Reboot rpi.
	4. Add LibreVNA service that runs the LibreVNA software in the background: 
		1. Copy service file to systemd: `sudo cp ~/Radar-LibreVNA-Firmware/librevna.service /lib/systemd/system/`
		2. Test service: `sudo systemctl start librevna.service`, `sudo systemctl stop librevna.service`
		3. Enable service at boot: `sudo systemctl enable librevna.service`
		4. Check for errors: `service librevna status`
	5. If sweeping automatically, reboot the vna and system. 
	6. Check that e.g. USER in the service file is set correctly. If service is modified, use `systemctl daemon-reload`.
	
	7. Run Python script to perform a single measurement: `python3 Wavetrax-VNA-firmware/librevna.py` <br>
	   ⚠️ Make sure the LibreVNA has enough power! ⚠️

2. PART II: Update embedded MCU
	1. Update VNA firmware via GUI `sudo QT_QPA_PLATFORM="vnc:size=1280x1280" ./LibreVNA-GUI --port 1234`
	2. Download desired embedded firmware version `sudo wget https://github.com/jankae/LibreVNA/releases/download/v1.6.2/EmbeddedFirmware-hw-rev-B-v1.6.2.zip`
	3. Update VNA firmware via the GUI --> VNC viewer (E.g. RealVNC Viewer)
	4. Remove files `sudo rm -rf VNA_embedded.elf` & `sudo rm -rf combined.vnafw`

3. PART III: Reduce default output power from -10 dBm to -35 dBm
   	1. Via LibreVNA GUI
	``` 
	cd /lib/systemd/system/
	sudo nano librevna.service
	```
   	 2. Select GUI version and reload systemd
	``` 
	sudo systemctl daemon-reexec
	sudo systemctl daemon-reload
	sudo systemctl restart librevna.service
	``` 
   	 4. Open VNA GUI via VNC viewer or via RMS --> Window --> Preferences --> Default Values --> Simulus level --> Change to -35 dBm
   	 5. Restart service `sudo systemctl restart librevna.service` and check via the GUI if deafult power level is changed
   	 6. Select no GUI version and reload systemd
	``` 
	sudo systemctl daemon-reexec
	sudo systemctl daemon-reload
	sudo systemctl restart librevna.service
	``` 

### 5️⃣ Connection with temperature sensors DS18B20
1. Connect D to GPIO4
2. Enable w1-gpio: add `dtoverlay=w1-gpio` with `sudo nano /boot/firmware/config.txt`
3. Reboot `sudo reboot`
4. Activate and test onewire:
	``` 
	sudo modprobe w1-gpio
	sudo modprobe w1-therm
	cd /sys/bus/w1/devices
	ls
	``` 
	If you see the sensor's ID as dir here, all is well. 

### 6️⃣ Install required python packages
Install pip `sudo apt install python3-pip`
```
sudo python3 -m pip install pyyaml --break-system-packages
sudo python3 -m pip install filelock --break-system-packages
sudo python3 -m pip install influxdb_client --break-system-packages
sudo python3 -m pip install flask --break-system-packages
sudo python3 -m pip install psutil --break-system-packages
``` 

### 7️⃣ Test all individual scripts
1. Test `system.py`
2. Test `librevna.py`
3. Test `controller.py`
4. Test `app.py`
5. Run services
	1. Copy service files to systemd:
	```
	sudo cp ~/Radar-LibreVNA-Firmware/app.service /lib/systemd/system/
	sudo cp ~/Radar-LibreVNA-Firmware/controller.service /lib/systemd/system/
 	```
 	2. Enable and start services
  	```
	sudo systemctl enable app.service
   	sudo systemctl enable controller.service
   	sudo systemctl start app.service
   	sudo systemctl start controller.service
  	```   

### 8️⃣ Test system data script
1. Download following packages
	`sudo python3 -m pip install psutil --break-system-packages`
2. Test `system.py`

### 9️⃣ Other features
1. Tailscale
	```
	curl -fsSL https://tailscale.com/install.sh | sh
	sudo apt update
	sudo apt install tailscale
	sudo tailscale up
	```
3. Samba (For debugging)
	1. Change <<<PASSWORD>>> and execute following commands
	   ```
	   curl -sSL https://get.docker.com | sh
	   sudo docker run -itd --name samba --restart=unless-stopped -p 139:139 -p 445:445 -v /home/pi:/mount dperson/samba -u "pi;<<<PASSWORD>>>" -s "pi;/mount;yes;no;no;pi"
	   sudo chmod 755 /home/pi
	   sudo chown pi:pi /home/pi
	   ```
    	2. Mount network drive \\IP_ADDRESS\pi and fill in credentials
