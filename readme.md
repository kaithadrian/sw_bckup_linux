install cifs-utils
/etc/fstab
//<IP>/network_device/tftp  /mnt/tftp  cifs  username=xxx,password=xxx,uid=switchbackup,gid=switchbackup,dir_mode=0770,file_mode=0660,vers=3.0,_netdev,nofail  0 0

For LXC, make sure container is Privilledged


secure enviroment
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

./venv/bin/python switch_backup.py 

python3 switch_backup.py -m tftp --tftp-server IP --tftp-root /mnt/tftp --require-mount