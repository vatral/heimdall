#!/usr/bin/env python3
import os
import string
import sys

from PyQt5 import QtCore, QtDBus
from PyQt5.QtCore import QTimer



if __name__ == "__main__":


    bus = QtDBus.QDBusConnection.sessionBus()
    msg = QtDBus.QDBusMessage.createMethodCall("com.troshchinskiy.Heimdall", "/heimdall", "", "ContextualAction")

    # QDBus doesn't like being given os.environ directly
    env = {}
    for var in os.environ:
        env[var] = os.environ[var]

    executed_command = sys.argv.copy()
    executed_command.pop(0)

    msg.setArguments([ env, os.getcwd(), " ".join(executed_command)])
    response = bus.call(msg)
    #print("Response: {}, {}".format(response.MessageType(), response.errorMessage()))
    #app.exec()

