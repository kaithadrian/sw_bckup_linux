Mount remote location:
  For LXC, make sure container is Privilledged
  install cifs-utils

Mount
/etc/fstab
//<IP>/network_device/tftp  /mnt/tftp  cifs  username=xxx,password=xxx,uid=switchbackup,gid=switchbackup,dir_mode=0770,file_mode=0660,vers=3.0,_netdev,nofail  0 0




Install dependencies to virtual env
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

Run
./venv/bin/python switch_backup.py 
./venv/bin/python switch_backup.py -m tftp --tftp-server IP --tftp-root /mnt/tftp --require-mount

