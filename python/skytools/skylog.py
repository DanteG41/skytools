"""Our log handlers for Python's logging package.
"""

from __future__ import division, absolute_import, print_function

import os
import socket
import time

import logging
import logging.handlers
from logging import LoggerAdapter

import skytools
import skytools.tnetstrings

try:
    unicode
except NameError:
    unicode = str   # noqa

__all__ = ['getLogger']

# add TRACE level
TRACE = 5
logging.TRACE = TRACE
logging.addLevelName(TRACE, 'TRACE')

# extra info to be added to each log record
_service_name = 'unknown_svc'
_job_name = 'unknown_job'
_hostname = socket.gethostname()
try:
    _hostaddr = socket.gethostbyname(_hostname)
except:
    _hostaddr = "0.0.0.0"
_log_extra = {
    'job_name': _job_name,
    'service_name': _service_name,
    'hostname': _hostname,
    'hostaddr': _hostaddr,
}
def set_service_name(service_name, job_name):
    """Set info about current script."""
    global _service_name, _job_name

    _service_name = service_name
    _job_name = job_name

    _log_extra['job_name'] = _job_name
    _log_extra['service_name'] = _service_name

#
# How to make extra fields available to all log records:
# 1. Use own getLogger()
#    - messages logged otherwise (eg. from some libs)
#      will crash the logging.
# 2. Fix record in own handlers
#    - works only with custom handlers, standard handlers will
#      crash is used with custom fmt string.
# 3. Change root logger
#    - can't do it after non-root loggers are initialized,
#      doing it before will depend on import order.
# 4. Update LogRecord.__dict__
#    - fails, as formatter uses obj.__dict__ directly.
# 5. Change LogRecord class
#    - ugly but seems to work.
#
_OldLogRecord = logging.LogRecord
class _NewLogRecord(_OldLogRecord):
    def __init__(self, *args):
        super(_NewLogRecord, self).__init__(*args)
        self.__dict__.update(_log_extra)
logging.LogRecord = _NewLogRecord


# configurable file logger
class EasyRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Easier setup for RotatingFileHandler."""
    def __init__(self, filename, maxBytes=10*1024*1024, backupCount=3):
        """Args same as for RotatingFileHandler, but in filename '~' is expanded."""
        fn = os.path.expanduser(filename)
        super(EasyRotatingFileHandler, self).__init__(fn, maxBytes=maxBytes, backupCount=backupCount)


# send JSON message over UDP
class UdpLogServerHandler(logging.handlers.DatagramHandler):
    """Sends log records over UDP to logserver in JSON format."""

    # map logging levels to logserver levels
    _level_map = {
        logging.DEBUG   : 'DEBUG',
        logging.INFO    : 'INFO',
        logging.WARNING : 'WARN',
        logging.ERROR   : 'ERROR',
        logging.CRITICAL: 'FATAL',
    }

    # JSON message template
    _log_template = '{\n\t'\
        '"logger": "skytools.UdpLogServer",\n\t'\
        '"timestamp": %.0f,\n\t'\
        '"level": "%s",\n\t'\
        '"thread": null,\n\t'\
        '"message": %s,\n\t'\
        '"properties": {"application":"%s", "apptype": "%s", "type": "sys", "hostname":"%s", "hostaddr": "%s"}\n'\
        '}\n'

    # cut longer msgs
    MAXMSG = 1024

    def makePickle(self, record):
        """Create message in JSON format."""
        # get & cut msg
        msg = self.format(record)
        if len(msg) > self.MAXMSG:
            msg = msg[:self.MAXMSG]
        txt_level = self._level_map.get(record.levelno, "ERROR")
        hostname = _hostname
        hostaddr = _hostaddr
        jobname = _job_name
        svcname = _service_name
        pkt = self._log_template % (time.time()*1000, txt_level, skytools.quote_json(msg),
                jobname, svcname, hostname, hostaddr)
        return pkt

    def send(self, s):
        """Disable socket caching."""
        sock = self.makeSocket()
        if not isinstance(s, bytes):
            s = s.encode('utf8')
        sock.sendto(s, (self.host, self.port))
        sock.close()


# send TNetStrings message over UDP
class UdpTNetStringsHandler(logging.handlers.DatagramHandler):
    """ Sends log records in TNetStrings format over UDP. """

    # LogRecord fields to send
    send_fields = [
        'created', 'exc_text', 'levelname', 'levelno', 'message', 'msecs', 'name',
        'hostaddr', 'hostname', 'job_name', 'service_name']

    _udp_reset = 0

    def makePickle(self, record):
        """ Create message in TNetStrings format.
        """
        msg = {}
        self.format(record) # render 'message' attribute and others
        for k in self.send_fields:
            msg[k] = record.__dict__[k]
        tnetstr = skytools.tnetstrings.dumps(msg)
        return tnetstr

    def send(self, s):
        """ Cache socket for a moment, then recreate it.
        """
        now = time.time()
        if now - 1 > self._udp_reset:
            if self.sock:
                self.sock.close()
            self.sock = self.makeSocket()
            self._udp_reset = now
        self.sock.sendto(s, (self.host, self.port))


class LogDBHandler(logging.handlers.SocketHandler):
    """Sends log records into PostgreSQL server.

    Additionally, does some statistics aggregating,
    to avoid overloading log server.

    It subclasses SocketHandler to get throtthling for
    failed connections.
    """

    # map codes to string
    _level_map = {
        logging.DEBUG   : 'DEBUG',
        logging.INFO    : 'INFO',
        logging.WARNING : 'WARNING',
        logging.ERROR   : 'ERROR',
        logging.CRITICAL: 'FATAL',
    }

    def __init__(self, connect_string):
        """
        Initializes the handler with a specific connection string.
        """

        super(LogDBHandler, self).__init__(None, None)
        self.closeOnError = 1

        self.connect_string = connect_string

        self.stat_cache = {}
        self.stat_flush_period = 60
        # send first stat line immediately
        self.last_stat_flush = 0

    def createSocket(self):
        try:
            super(LogDBHandler, self).createSocket()
        except:
            self.sock = self.makeSocket()

    def makeSocket(self, timeout=1):
        """Create server connection.
        In this case its not socket but database connection."""

        db = skytools.connect_database(self.connect_string)
        db.set_isolation_level(0) # autocommit
        return db

    def emit(self, record):
        """Process log record."""

        # we do not want log debug messages
        if record.levelno < logging.INFO:
            return

        try:
            self.process_rec(record)
        except (SystemExit, KeyboardInterrupt):
            raise
        except:
            self.handleError(record)

    def process_rec(self, record):
        """Aggregate stats if needed, and send to logdb."""
        # render msg
        msg = self.format(record)

        # dont want to send stats too ofter
        if record.levelno == logging.INFO and msg and msg[0] == "{":
            self.aggregate_stats(msg)
            if time.time() - self.last_stat_flush >= self.stat_flush_period:
                self.flush_stats(_job_name)
            return

        if record.levelno < logging.INFO:
            self.flush_stats(_job_name)

        # dont send more than one line
        ln = msg.find('\n')
        if ln > 0:
            msg = msg[:ln]

        txt_level = self._level_map.get(record.levelno, "ERROR")
        self.send_to_logdb(_job_name, txt_level, msg)

    def aggregate_stats(self, msg):
        """Sum stats together, to lessen load on logdb."""

        msg = msg[1:-1]
        for rec in msg.split(", "):
            k, v = rec.split(": ")
            agg = self.stat_cache.get(k, 0)
            if v.find('.') >= 0:
                agg += float(v)
            else:
                agg += int(v)
            self.stat_cache[k] = agg

    def flush_stats(self, service):
        """Send acquired stats to logdb."""
        res = []
        for k, v in self.stat_cache.items():
            res.append("%s: %s" % (k, str(v)))
        if len(res) > 0:
            logmsg = "{%s}" % ", ".join(res)
            self.send_to_logdb(service, "INFO", logmsg)
        self.stat_cache = {}
        self.last_stat_flush = time.time()

    def send_to_logdb(self, service, level, msg):
        """Actual sending is done here."""

        if self.sock is None:
            self.createSocket()

        if self.sock:
            logcur = self.sock.cursor()
            query = "select * from log.add(%s, %s, %s)"
            logcur.execute(query, [level, service, msg])


# fix unicode bug in SysLogHandler
class SysLogHandler(logging.handlers.SysLogHandler):
    """Fixes unicode bug in logging.handlers.SysLogHandler."""

    # be compatible with both 2.6 and 2.7
    socktype = socket.SOCK_DGRAM

    _udp_reset = 0

    def _custom_format(self, record):
        msg = self.format(record) + '\000'

        # We need to convert record level to lowercase, maybe this will
        # change in the future.
        prio = '<%d>' % self.encodePriority(self.facility,
                                            self.mapPriority(record.levelname))
        msg = prio + msg
        return msg

    def emit(self, record):
        """
        Emit a record.

        The record is formatted, and then sent to the syslog server. If
        exception information is present, it is NOT sent to the server.
        """
        msg = self._custom_format(record)
        # Message is a string. Convert to bytes as required by RFC 5424
        if isinstance(msg, unicode):
            msg = msg.encode('utf-8')
            ## this puts BOM in wrong place
            #if codecs:
            #    msg = codecs.BOM_UTF8 + msg
        try:
            if self.unixsocket:
                try:
                    self.socket.send(msg)
                except socket.error:
                    self._connect_unixsocket(self.address)
                    self.socket.send(msg)
            elif self.socktype == socket.SOCK_DGRAM:
                now = time.time()
                if now - 1 > self._udp_reset:
                    self.socket.close()
                    self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._udp_reset = now
                self.socket.sendto(msg, self.address)
            else:
                self.socket.sendall(msg)
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            self.handleError(record)

class SysLogHostnameHandler(SysLogHandler):
    """Slightly modified standard SysLogHandler - sends also hostname and service type"""

    def _custom_format(self, record):
        msg = self.format(record)
        format_string = '<%d> %s %s %s\000'
        msg = format_string % (self.encodePriority(self.facility, self.mapPriority(record.levelname)),
                               _hostname, _service_name, msg)
        return msg


# add missing aliases (that are in Logger class)
if not hasattr(LoggerAdapter, 'fatal'):
    LoggerAdapter.fatal = LoggerAdapter.critical
if not hasattr(LoggerAdapter, 'warn'):
    LoggerAdapter.warn = LoggerAdapter.warning

class SkyLogger(LoggerAdapter):
    def __init__(self, logger, extra):
        super(SkyLogger, self).__init__(logger, extra)
        self.name = logger.name
    def trace(self, msg, *args, **kwargs):
        """Log 'msg % args' with severity 'TRACE'."""
        self.log(TRACE, msg, *args, **kwargs)
    def addHandler(self, hdlr):
        """Add the specified handler to this logger."""
        self.logger.addHandler(hdlr)
    def isEnabledFor(self, level):
        """See if the underlying logger is enabled for the specified level."""
        return self.logger.isEnabledFor(level)

def getLogger(name=None, **kwargs_extra):
    """Get logger with extra functionality.

    Adds additional log levels, and extra fields to log record.

    name - name for logging.getLogger()
    kwargs_extra - extra fields to add to log record
    """
    log = logging.getLogger(name)
    return SkyLogger(log, kwargs_extra)
