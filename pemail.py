import smtplib
from email.mime.text import MIMEText
import sys
import datetime
import json
import os

with open(os.path.join(os.path.dirname(__file__), 'secret.json'), 'r') as f:
    secret = json.load(f)

sender = secret['sender']
password = secret['password']
receiver = secret['receiver']

def apply_success(card_name, start_time, end_time, trials):
    duration = end_time - start_time
    msg = MIMEText("The card {} has been successfully created, after applying for {} (totally {} trials) EOM".format(card_name, duration, trials))
    msg["Subject"] = "Card {} created".format(card_name)
    msg["From"] = sender
    msg["To"] = receiver
    return msg

def apply_fail(card_name, start_time, end_time, trials):
    card_name = '-'.join(card_name.split('-')[-2:])
    start_time = datetime.datetime.strptime(start_time, "%a %b %d %H:%M:%S %Z %Y")
    end_time = datetime.datetime.strptime(end_time, "%a %b %d %H:%M:%S %Z %Y")
    
    duration = end_time - start_time
    msg = MIMEText("The card {} has NOT been created after applying for {} (totally {} trials) EOM".format(card_name, duration, trials))
    msg["Subject"] = "Card {} creation FAILED".format(card_name)
    msg["From"] = sender
    msg["To"] = receiver
    return msg

def queue_start(stage_dir, start_time, end_time, vm_name):
    card_name = '-'.join(vm_name.split('-')[-2:])
    start_time = datetime.datetime.strptime(start_time, "%Y%m%d_%H%M%S")
    end_time = datetime.datetime.strptime(end_time, "%a %b %d %H:%M:%S %Z %Y")
    duration = end_time - start_time

    msg = MIMEText("The job at {}, that has been queued at {}, is now starting on {}. (totally {} since queued). EOM".format(stage_dir, start_time, card_name, duration))
    msg["Subject"] = "Job at {} starting on {}".format(stage_dir, card_name)
    msg["From"] = sender
    msg["To"] = receiver
    return msg

cmd = sys.argv[1].lstrip('--').replace('-', '_')
msg = globals()[cmd](*sys.argv[2:])

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(sender, password)
    server.sendmail(sender, [receiver], msg.as_string())

print("Email sent successfully to {}".format(receiver))