#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
from PyQt5 import QtCore, QtDBus
from PyQt5.QtCore import QObject, QTimer, Q_CLASSINFO, pyqtSlot, pyqtProperty, QProcess, pyqtSignal
from PyQt5.QtDBus import QDBusAbstractAdaptor
from subprocess import Popen, PIPE
import json

from i3ipc import Connection

class ContextualExecutor:
    def __init__(self, parent):
        self.parent = parent
        self.prev_path = ""


    def execute(self, environment, path, command):
        print("Contextual action")
        print("ENV    : {}".format(environment))
        print("Path   : " + path);
        print("Command: " + command)

        if path != self.prev_path:
            prev_path = path

            self.parent.i3_command("[workspace='99'] kill, [workspace='99: contextual'] kill")
            self.parent.i3_command("workspace 99")
            self.parent.i3_command("rename workspace '99' to '99: contextual'")
            self.parent.i3_command('exec $TERM_EXEC_KEEP $SSH_TO_HOST ls -l --color "' + path + '"')


class DBusAdaptor(QDBusAbstractAdaptor):
    Q_CLASSINFO("D-Bus Interface", "com.troshchinskiy.Heimdall")
    Q_CLASSINFO("D-Bus Introspection",
                '<interface name="com.troshchinskiy.Heimdall">\n'
                '    <property name="Version" type="s" access="read"/>\n'
                '    <method name="Connect"/>\n'
                '    <method name="Disconnect"/>\n'
                '    <method name="Reset"/>\n'
                '    <method name="ContextualAction">\n'
                '        <arg direction="in" type="a{ss}" name="environment"/>\n'
                '        <arg direction="in" type="s" name="path"/>\n'
                '        <arg direction="in" type="s" name="command"/>\n'
                '    </method>\n'
                '    <method name="echo">\n'
                '        <arg direction="in" type="s" name="text"/>\n'
                '        <arg direction="out" type="s" name="return"/>\n'
                '    </method>\n'
                '</interface>\n'
                )
    def __init__(self, parent):
        super().__init__(parent)

    @pyqtSlot(str, result=str, name="echo")
    def echo(self, text):
        print("Echo called!")
        return self.parent().echo(text)

    @pyqtProperty(str)
    def Version(self):
        print("Version called!")
        return self.parent().version()

    @pyqtSlot(name="Connect")
    def Connect(self):
        print("Connect called!")
        self.parent().start_tunnel()

    @pyqtSlot(name="Disconnect")
    def Disconnect(self):
        print("Disconnect called!")
        self.parent().stop_tunnel()

    @pyqtSlot('QVariantMap', str, str)
    def ContextualAction(self, environment, path, command):
        print("ContextualAction called!")
        self.parent().contextual_action(environment, path, command)

    @pyqtSlot(name="Reset")
    def Reset(self):
        print("Reset called!")
        self.parent().stop_tunnel()
        self.parent().start_tunnel()

class Heimdall(QObject):
    """Heimdall service

    This service maintains a Raspberry Pi or similar device as an automatic display that works without user interaction.

    Connection timeline:
        0. _setup_reestablish_tunnel - Set up a timer to try to reestablish the tunnel. This step is only performed
           if the tunnel was previously active and failed, and exist to provide a reconnection delay.

        1. start_tunnel - Run SSH to the Raspberry pi

        2. try_connect - Attempt to use the SSH connection to contact i3/Sway.
           If this fails, an automatic retry after a timeout is done.

        3. setup - Take over the i3/Sway setup

    """
    def __init__(self, parent = None):
        super().__init__(parent)

        self.reconnect_timer = QTimer()
        self.ssh_proc = QProcess()

        self.bus = QtDBus.QDBusConnection.sessionBus()
        self.dbus_adaptor = DBusAdaptor(self)
        self.contextual_executor = ContextualExecutor(self)

        if not self.bus.isConnected():
            raise Exception("Failed to connect to dbus!")

        self.bus.registerObject("/heimdall", self)
        self.bus.registerService("com.troshchinskiy.Heimdall")

        self.homedir = os.environ['HOME'] + "/.heimdall"
        self.read_config()
        self.start_tunnel()

    def echo(self, text):
        return text

    def version(self):
        return "0.1"

    def connect(self):
        self.ssh = Popen(["ssh"], stdout=PIPE)

    def read_config(self):
        filename = self.homedir + '/config.json'

        print("Loading config file {}...\n".format(filename))

        with open(filename, 'r') as conf_h:
            self.config = json.load(conf_h)

    def start_tunnel(self):
        if self.ssh_proc and self.ssh_proc.isOpen():
            print("Tunnel already running")
            return

        print("Starting tunnel...\n")

        sway_pid = self._run_remote(["pidof", "sway"])
        if sway_pid is None:
            raise Exception('Sway is not running!')

        home_dir = self._run_remote(["echo", '$HOME'])
        uid      = self._run_remote(["echo", '$UID'])

        self.remote_socket = "/run/user/" + uid + "/sway-ipc." + uid + "." + sway_pid + ".sock"
        self.local_socket = self.homedir + "/sway.sock"

        print("Sway pid: '{}'".format(sway_pid))
        print("Home dir: '{}'".format(home_dir))
        print("UID     : '{}'".format(uid))
        print("Socket  : '{}'".format(self.remote_socket))

        if os.path.exists(self.local_socket):
            os.remove(self.local_socket)

        r = self.config['remote']

        command_args = [
                    "-i", r['ssh-key'],
                    "-p", r['port'],
                    "-l", r['user'],
                    "-R", r['backwards-port'] + ":127.0.0.1:" + r['local-ssh-port'],
                    "-L", self.local_socket + ':' + self.remote_socket,
                    r['server']]

        print("Running command: ssh {}".format(command_args))

        self.ssh_proc.started.connect(self._ssh_process_started)
        self.ssh_proc.errorOccurred.connect(self._ssh_process_error)
        self.ssh_proc.finished.connect(self._ssh_process_finished)

        self.ssh_proc.start(self.config['commands']['ssh'], command_args)

    def try_connect(self):
        """Try to connect to i3/Sway.

        SSH takes a while to perform the port forwarding, so we may do this several times, until it starts
        working.
        """
        print("Trying to connect to Sway/i3 at socket {}...".format(self.local_socket))
        try:
            self.i3 = Connection(socket_path=self.local_socket)
        except ConnectionRefusedError:
            print("Not connected yet!")
            return
        except FileNotFoundError:
            print("Socket doesn't exist yet!!")
            return

        self.connect_timer.stop()
        self.setup()

    def setup(self):
        try:
            print("Setting up Sway/i3...")
            self.wm_version = self.i3.get_version()
            print("Connected to Sway/i3 version {}".format(self.wm_version))

            print("Resetting workspace...")
            for workspace in self.i3.get_workspaces():
                print("Deleting workspace {}".format(workspace.name))
                self.i3.command('[workspace="{}"] kill'.format(workspace.name))

            print("Executing commands...")
            for cmd in self.config['startup']['remote-run']:
                print("\tExecuting: {}".format(cmd))
                self._run_remote(cmd)

            print("Setting up workspaces...")
            wsnum=0
            for wsconf in self.config['startup']['workspaces']:
                wsnum+=1
                self.i3.command("workspace {}".format(wsnum))
                self.i3.command('rename workspace "{}" to "{}"'.format(wsnum, wsconf['name']))

                for wscmd in wsconf['commands']:
                    self.i3_command(wscmd)

        except (ConnectionRefusedError, FileNotFoundError):
            self._setup_reestablish_tunnel()

    def i3_command(self, command):


        command = command.replace('$TERM_EXEC_KEEP', self.config['remote']['terminal-exec-keep'])
        command = command.replace('$TERM_EXEC', self.config['remote']['terminal-exec'])
        command = command.replace('$TERM', self.config['remote']['terminal'])
        command = command.replace('$SSH_TO_HOST',
                        self.config['commands']['ssh'] +
                        " -p " + self.config['remote']['backwards-port'] +
                        " -t " +
                        os.environ['USER'] + '@localhost ')

        print("Executing command: " + command)
        self.i3.command(command)

    def contextual_action(self, environment, path, command):
        self.contextual_executor.execute(environment, path, command)

    def stop_tunnel(self):
        """Stop the tunnel, if it's running"""

        if self.ssh_proc and self.ssh_proc.isOpen():
            print("Stopping ssh\n")
            self.ssh_proc.kill()
            self.ssh_proc.close()

        if os.path.exists(self.local_socket):
            os.remove(self.local_socket)


    def _setup_reestablish_tunnel(self):
        """Re-establish the SSH tunnel and begin again the process of syncing up"""

        self.stop_tunnel()
        self.reconnect_timer.timeout.connect(self.start_tunnel())
        self.reconnect_timer.singleShot(True)
        self.reconnect_timer.start(100)

    def _ssh_process_started(self):
        print("SSH process started!")
        self.connect_timer = QTimer()
        self.connect_timer.timeout.connect(self.try_connect)
        self.connect_timer.start(50);


    def _ssh_process_error(self, error):
        print("SSH process failed with error {}!".format(error))

    def _ssh_process_finished(self, exit_code, exit_status):
        print("SSH process exited with code {}, status {}!".format(exit_code, exit_status))

    def _run_remote(self, command):
        r = self.config['remote']

        ssh_command = [self.config['commands']['ssh'],
                       "-i", r['ssh-key'],
                       "-p", r['port'],
                       "-l", r['user'],
                       r['server']];
        ssh_command += command

        print("Running: {}".format(ssh_command))
        result_raw = subprocess.run(ssh_command, stdout=subprocess.PIPE)
        result = result_raw.stdout.decode('utf-8').strip()
        return result



def abort(signum, frame):
    print("Signal {} received, aborting".format(signum))
    heimdall.stop_tunnel()
    app.exit(2)

if __name__ == "__main__":
    app = QtCore.QCoreApplication(sys.argv)

    # Ugly hack to make Ctrl+C work correctly
    # https://stackoverflow.com/questions/4938723/what-is-the-correct-way-to-make-my-pyqt-application-quit-when-killed-from-the-co
    timer = QTimer()
    timer.start(500);
    timer.timeout.connect(lambda : None)

    print("Starting...\n")
    heimdall = Heimdall()
    signal.signal(signal.SIGINT, abort)
    signal.signal(signal.SIGTERM, abort)

    app.exec()
    print("Done!")

