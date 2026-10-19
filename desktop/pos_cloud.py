"""Paprika POS Cloud: the cash register app with the cloud server address built in.

Same program as pos_app.py, but it connects only to CLOUD_SERVER_URL and asks just for
the register key. Settings are in pos-cloud-config.json, the log in pos-cloud-app.log.
"""
import pos_app

CLOUD_SERVER_URL = "https://pos.mypaprika.net"

if __name__ == "__main__":
    pos_app.main(CLOUD_SERVER_URL, name="pos-cloud")
