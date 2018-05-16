
#  Copyright (c) 2018 Sony Pictures Imageworks Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.



"""
Main RQD module, handles ICE function implmentation and job launching.

Project: RQD

Module: rqcore.py

Contact: Middle-Tier (middle-tier@imageworks.com)

SVN: $Id$
"""

import os
import sys
import subprocess
import time
import threading
import logging as log
import traceback
import platform
import tempfile

if platform.system() == 'Linux':
    from grp import getgrnam

import rqconstants
import rqutil

from rqnetwork import Network
from rqnetwork import RunningFrame
from rqnetwork import RqdIceException
from rqmachine import Machine
from rqnimby import Nimby

import Ice
Ice.loadSlice("--all -I{PATH}/slice/spi -I{PATH}/slice/cue {PATH}/slice/cue/" \
              "rqd_ice.ice".replace("{PATH}", os.path.dirname(__file__)))
import cue.RqdIce as RqdIce
import spi.SpiIce as SpiIce
from cue.CueIce import HardwareState

class FrameAttendantThread(threading.Thread):
    """Once a frame has been recieved and checked by RQD, this class handles
       the launching, waiting on, and cleanup work related to running the
       frame."""
    def __init__(self, rq_core, runFrame, frameInfo):
        """FrameAttendantThread class initialization
           @type    rq_core: RqCore
           @param   rq_core: Main RQD Object
           @type   runFrame: RunFrame
           @param  runFrame: Struct from cuebot
           @type  frameInfo: RunningFrame
           @param frameInfo: Servant for running frame
        """
        threading.Thread.__init__(self)
        self.rq_core = rq_core
        self.frameId = runFrame.frameId
        self.runFrame = runFrame
        self.frameInfo = frameInfo
        self._tempLocations = []

    def __createEnvVariables(self):
        """Define the environmental variables for the frame"""
        # If linux specific, they need to move into self.runLinux()
        self.frame_env = {}
        self.frame_env["PATH"] = self.rq_core.machine.getPathEnv()
        self.frame_env["TERM"] = "unknown"
        self.frame_env["TZ"] = self.rq_core.machine.getTimezone()
        self.frame_env["USER"] = self.runFrame.userName
        self.frame_env["LOGNAME"] = self.runFrame.userName
        self.frame_env["MAIL"] = "/usr/mail/%s" % self.runFrame.userName
        self.frame_env["HOME"] = "/net/homedirs/%s" % self.runFrame.userName
        self.frame_env["mcp"] = "1"
        self.frame_env["show"] = self.runFrame.show
        self.frame_env["shot"] = self.runFrame.shot
        self.frame_env["jobid"] = self.runFrame.jobName
        self.frame_env["jobhost"] = self.rq_core.machine.getHostname()
        self.frame_env["frame"] = self.runFrame.frameName
        self.frame_env["zframe"] = self.runFrame.frameName
        self.frame_env["logfile"] = self.runFrame.logFile
        self.frame_env["maxframetime"] = "0"
        self.frame_env["minspace"] = "200"
        self.frame_env["CUE3"] = "True"
        self.frame_env["CUE_GPU_MEMORY"] = str(self.rq_core.machine.getGpuMemory())
        self.frame_env["SP_NOMYCSHRC"] = "1"

        for key in self.runFrame.environment:
            self.frame_env[key] = self.runFrame.environment[key]

        # Add threads to use all assigned hyper-threading cores
        if self.runFrame.attributes.has_key('CPU_LIST') and self.frame_env.has_key('CUE_THREADS'):
            self.frame_env['CUE_THREADS'] = str(max(int(self.frame_env['CUE_THREADS']),
                                                    len(self.runFrame.attributes['CPU_LIST'].split(','))))
            self.frame_env['CUE_HT'] = "True"

    def _create_command_file(self, command):
        """Creates a file that subprocess. Popen then executes.
        @type  command: string
        @param command: The command specified in the runFrame request
        @rtype:  string
        @return: Command file location"""
        try:
            if platform.system() == "Windows":
                commandFile = os.path.join('C:\\temp',
                                           'rqd-cmd-%s-%s.bat' % (self.runFrame.frameId, time.time()))
            else: 
                commandFile = os.path.join(tempfile.gettempdir(),
                                           'rqd-cmd-%s-%s' % (self.runFrame.frameId, time.time()))
            rqexe = open(commandFile, "w")
            self._tempLocations.append(commandFile)
            rqexe.write(command)
            rqexe.close()
            os.chmod(commandFile, 0777)
            return commandFile
        except Exception, e:
            log.critical("Unable to make command file: %s due to %s at %s" % \
                                      (commandFile, e,
                                       traceback.extract_tb(sys.exc_info()[2])))

    def __writeHeader(self):
        """Writes the frame's log header"""
        rqlog = self.runFrame.rqlog

        self.frameInfo.startTime = time.time()

        try:
            print >> rqlog, "="*59
            print >> rqlog, "RenderQ JobSpec     ", \
                            time.ctime(self.frameInfo.startTime), "\n"
            print >> rqlog, "proxy               ", \
                            "RunningFrame/%s -t:tcp -h %s -p 10021" % \
                            (self.runFrame.frameId,
                             self.rq_core.machine.getHostname())
            print >> rqlog, "%-21s%s" % ("command", self.runFrame.command)
            print >> rqlog, "%-21s%s" % ("uid", self.runFrame.uid)
            print >> rqlog, "%-21s%s" % ("gid", self.runFrame.gid)
            print >> rqlog, "%-21s%s" % ("logDestination",
                                         self.runFrame.logDirFile)
            print >> rqlog, "%-21s%s" % ("cwd", self.runFrame.frameTempDir)
            print >> rqlog, "%-21s%s" % ("renderHost",
                                         self.rq_core.machine.getHostname())
            print >> rqlog, "%-21s%s" % ("jobId", self.runFrame.jobId)
            print >> rqlog, "%-21s%s" % ("frameId", self.runFrame.frameId)
            for env in sorted(self.frame_env):
                print >> rqlog, "%-21s%s=%s" % ("env", env, self.frame_env[env])
            print >> rqlog, "="*59

            if self.runFrame.attributes.has_key('CPU_LIST'):
                print >> rqlog, 'Hyper-threading enabled'

        except Exception, e:
            log.critical("Unable to write header to rqlog: "
                         "%s due to %s at %s" % \
                         (self.runFrame.logDirFile, e,
                         traceback.extract_tb(sys.exc_info()[2])))

    def __writeFooter(self):
        """Writes frame's log footer"""
        rqlog = self.runFrame.rqlog

        self.frameInfo.endTime = time.time()
        self.frameInfo.runTime = int(self.frameInfo.endTime -
                                     self.frameInfo.startTime)
        try:
            print >> rqlog, "\n", "="*59
            print >> rqlog, "RenderQ Job Complete\n"
            print >> rqlog, "%-20s%s" % ("exitStatus",
                                         self.frameInfo.exitStatus)
            print >> rqlog, "%-20s%s" % ("exitSignal",
                                         self.frameInfo.exitSignal)
            if self.frameInfo.killMessage:
                print >> rqlog, "%-20s%s" % ("killMessage",
                                             self.frameInfo.killMessage)
            print >> rqlog, "%-20s%s" % ("startTime",
                                         time.ctime(self.frameInfo.startTime))
            print >> rqlog, "%-20s%s" % ("endTime",
                                         time.ctime(self.frameInfo.endTime))
            print >> rqlog, "%-20s%s" % ("maxrss", self.frameInfo.maxRss)
            print >> rqlog, "%-20s%s" % ("utime", self.frameInfo.utime)
            print >> rqlog, "%-20s%s" % ("stime", self.frameInfo.stime)
            print >> rqlog, "%-20s%s" % ("renderhost",
                                         self.rq_core.machine.getHostname())
            print >> rqlog, "="*59
        except Exception, e:
            log.critical("Unable to write footer: %s due to %s at %s" % \
                                   (self.runFrame.logDirFile, e,
                                    traceback.extract_tb(sys.exc_info()[2])))

    def __sendFrameCompleteReport(self):
        """Send report to cuebot that frame has finished"""
        report = RqdIce.FrameCompleteReport()
        report.host = self.rq_core.machine.getHostInfo()
        report.frame = self.frameInfo.runningFrameInfo()

        if self.frameInfo.exitStatus is None:
            report.exitStatus = 1
        else:
            report.exitStatus = self.frameInfo.exitStatus

        report.exitSignal = self.frameInfo.exitSignal
        report.runTime = int(self.frameInfo.runTime)

        # If nimby is active, then frame must have been killed by nimby
        # Set the exitSignal to indicate this event
        if self.rq_core.nimby.locked and not self.runFrame.ignoreNimby:
            report.exitStatus = rqconstants.EXITSTATUS_FOR_NIMBY_KILL

        self.rq_core.network.reportRunningFrameCompletion(report)

    def __cleanup(self):
        """Cleans up temporary files"""
        rqutil.permissionsHigh()
        try:
            for location in self._tempLocations:
                if os.path.isfile(location):
                    try:
                        os.remove(location)
                    except Exception, e:
                        log.warning("Unable to delete file: %s due to %s at %s" % \
                                     (location, e,
                                     traceback.extract_tb(sys.exc_info()[2])))
        finally:
            rqutil.permissionsLow()

        # Close log file
        try:
            self.runFrame.rqlog.close()
        except Exception, e:
            log.warning("Unable to close file: %s due to %s at %s" % \
                         (self.runFrame.logFile, e,
                          traceback.extract_tb(sys.exc_info()[2])))

    def runLinux(self):
        """The steps required to handle a frame under linux"""
        frameInfo = self.frameInfo
        runFrame = self.runFrame

        self.__createEnvVariables()
        self.__writeHeader()

        tempStatFile = "%srqd-stat-%s-%s" % (self.rq_core.machine.getTempPath(),
                                             frameInfo.frameId,
                                             time.time())
        self._tempLocations.append(tempStatFile)
        tempCommand = []
        if self.rq_core.machine.isDesktop():
            tempCommand += ["/bin/nice"]
        tempCommand += ["/usr/bin/time", "-p", "-o", tempStatFile]

        if runFrame.attributes.has_key('CPU_LIST'):
            tempCommand += ['taskset', '-c', runFrame.attributes['CPU_LIST']]

        rqutil.permissionsHigh()
        try:
            tempCommand += ["/bin/su", runFrame.userName, rqconstants.SU_ARGUEMENT,
                            '"' + self._create_command_file(runFrame.command) + '"']

            # Actual cwd is set by /shots/SHOW/home/perl/etc/qwrap.cuerun
            frameInfo.forkedCommand = subprocess.Popen(tempCommand,
                                                       env=self.frame_env,
                                                       cwd=self.rq_core.machine.getTempPath(),
                                                       stdin=subprocess.PIPE,
                                                       stdout=runFrame.rqlog,
                                                       stderr=runFrame.rqlog,
                                                       close_fds=True,
                                                       preexec_fn = os.setsid)
        finally:
            rqutil.permissionsLow()

        frameInfo.pid = frameInfo.forkedCommand.pid

        if not self.rq_core.updateRssThread.isAlive():
            self.rq_core.updateRssThread = threading.Timer(rqconstants.RSS_UPDATE_INTERVAL,
                                                           self.rq_core.updateRss)
            self.rq_core.updateRssThread.start()

        returncode = frameInfo.forkedCommand.wait()

        # Find exitStatus and exitSignal
        if returncode < 0:
            # Exited with a signal
            frameInfo.exitStatus = 1
            frameInfo.exitSignal = -returncode
        else:
            frameInfo.exitStatus = returncode
            frameInfo.exitSignal = 0

        try:
            statFile  = open(tempStatFile,"r")
            frameInfo.realtime = statFile.readline().split()[1]
            frameInfo.utime = statFile.readline().split()[1]
            frameInfo.stime = statFile.readline().split()[1]
            statFile.close()
        except Exception:
            pass # This happens when frames are killed

        self.__writeFooter()
        self.__cleanup()

    def runWin32(self):
        """The steps required to handle a frame under windows"""
        pass

    def runWindows(self):
        """The steps required to handle a frame under windows"""
        frameInfo = self.frameInfo
        runFrame = self.runFrame

        self.__createEnvVariables()
        self.__writeHeader()

        try:
            runFrame.command = runFrame.command.replace('%{frame}', self.frame_env['CUE_IFRAME'])
            tempCommand = [self._create_command_file(runFrame.command)]

            frameInfo.forkedCommand = subprocess.Popen(tempCommand,
                                                       stdin=subprocess.PIPE,
                                                       stdout=runFrame.rqlog,
                                                       stderr=runFrame.rqlog)
        except:
            log.critical("Failed subprocess.Popen: Due to: \n%s" % \
                         ''.join(traceback.format_exception(*sys.exc_info())))

        frameInfo.pid = frameInfo.forkedCommand.pid

        if not self.rq_core.updateRssThread.isAlive():
            self.rq_core.updateRssThread = threading.Timer(rqconstants.RSS_UPDATE_INTERVAL,
                                                           self.rq_core.updateRss)
            self.rq_core.updateRssThread.start()

        frameInfo.forkedCommand.wait()

        # Find exitStatus and exitSignal
        returncode = frameInfo.forkedCommand.returncode
        frameInfo.exitStatus = returncode
        frameInfo.exitSignal = returncode

        frameInfo.realtime = 0
        frameInfo.utime = 0
        frameInfo.stime = 0

        self.__writeFooter()
        self.__cleanup()

    def runDarwin(self):
        """The steps required to handle a frame under mac"""
        frameInfo = self.frameInfo

        self.__createEnvVariables()
        self.__writeHeader()

        rqutil.permissionsHigh()
        try:
            tempCommand = ["/usr/bin/su", frameInfo.userName, "-c", '"' +
                           self._create_command_file(frameInfo.command) + '"']

            frameInfo.forkedCommand = subprocess.Popen(tempCommand,
                                                       env=self.frame_env,
                                                       cwd=self.rq_core.machine.getTempPath(),
                                                       stdin=subprocess.PIPE,
                                                       stdout=frameInfo.rqlog,
                                                       stderr=frameInfo.rqlog,
                                                       preexec_fn = os.setsid)
        finally:
            rqutil.permissionsLow()

        frameInfo.pid = frameInfo.forkedCommand.pid

        if not self.rq_core.updateRssThread.isAlive():
            self.rq_core.updateRssThread = threading.Timer(rqconstants.RSS_UPDATE_INTERVAL,
                                                         self.rq_core.updateRss)
            self.rq_core.updateRssThread.start()

        frameInfo.forkedCommand.wait()

        # Find exitStatus and exitSignal
        returncode = frameInfo.forkedCommand.returncode
        if (os.WIFEXITED(returncode)):
            frameInfo.exitStatus = os.WEXITSTATUS(returncode)
        else:
            frameInfo.exitStatus = 1
        if os.WIFSIGNALED(returncode):
            frameInfo.exitSignal = os.WTERMSIG(returncode)

        self.__writeFooter()
        self.__cleanup()

    def runUnknown(self):
        """The steps required to handle a frame under an unknown OS"""
        pass

    def run(self):
        """Thread initilization"""
        log.info("Monitor frame started for frameId=%s", self.frameId)

        runFrame = self.runFrame

        # Windows has a special log path
        if platform.system() == "Windows":
            runFrame.logDir = '//intrender/render/logs/%s--%s' % (runFrame.jobName, runFrame.jobId)

        try:
            runFrame.jobTempDir = os.path.join(self.rq_core.machine.getTempPath(),
                                               runFrame.jobName)
            runFrame.frameTempDir = os.path.join(runFrame.jobTempDir,
                                                 runFrame.frameName)
            runFrame.logFile = "%s.%s.rqlog" % (runFrame.jobName,
                                                runFrame.frameName)
            runFrame.logDirFile = os.path.join(runFrame.logDir, runFrame.logFile)

            try: # Exception block for all exceptions
                # Do everything as launching user
                runFrame.gid = rqconstants.LAUNCH_FRAME_USER_GID

                # Change to job user
                rqutil.permissionsUser(runFrame.uid, runFrame.gid)
                try:
                    #
                    # Setup proc to allow launching of frame
                    #

                    if not os.access(runFrame.logDir, os.F_OK):
                        # Attempting mkdir for missing logdir
                        msg = "No Error"
                        try:
                            os.mkdir(runFrame.logDir)
                            os.chmod(runFrame.logDir, 0777)
                        except Exception, e:
                            # This is expected to fail when called in abq
                            # But the directory should now be visible
                            msg = e

                        if not os.access(runFrame.logDir, os.F_OK):
                            err = "Unable to see log directory: %s, mkdir " \
                                  "failed with: %s" % (runFrame.logDir, msg)
                            raise RuntimeError, err

                    if not os.access(runFrame.logDir, os.W_OK):
                        err = "Unable to write to log directory %s" % \
                              runFrame.logDir
                        raise RuntimeError, err

                    try:
                        # Rotate any old logs to a max of MAX_LOG_FILES:
                        if os.path.isfile(runFrame.logDirFile):
                            rotateCount = 1
                            while os.path.isfile("%s.%s" % (runFrame.logDirFile,
                                                            rotateCount)) \
                                  and rotateCount < rqconstants.MAX_LOG_FILES:
                                rotateCount += 1
                            os.rename(runFrame.logDirFile,
                                      "%s.%s" % (runFrame.logDirFile, rotateCount))
                    except Exception, e:
                        err = "Unable to rotate previous log file due to %s" % e
                        raise RuntimeError, err
                    try:
                        runFrame.rqlog = file(runFrame.logDirFile, "w", 0)
                        os.chmod(runFrame.logDirFile, 0666)
                    except Exception, e:
                        err = "Unable to write to %s due to %s" % (runFrame.logDirFile, e)
                        raise RuntimeError, err
                finally:
                    rqutil.permissionsLow()

                # Store frame in cache and register servant
                self.rq_core.storeFrame(runFrame.frameId, self.frameInfo)

                if platform.system() == "Linux":
                    self.runLinux()
                elif platform.system() == "win32":
                    self.runWin32()
                elif platform.system() == "Windows":
                    self.runWindows()
                elif platform.system() == "Darwin":
                    self.runDarwin()
                else:
                    self.runUnknown()

            except Exception, e:
                log.critical("Failed launchFrame: For %s due to: \n%s" % \
                             (runFrame.frameId,
                              ''.join(traceback.format_exception(*sys.exc_info()))))
                # Notifies the cuebot that there was an error launching
                self.frameInfo.exitStatus = rqconstants.EXITSTATUS_FOR_FAILED_LAUNCH
                # Delay keeps the cuebot from spamming failing booking requests
                time.sleep(10)
        finally:
            self.rq_core.releaseCores(self.runFrame.numCores, runFrame.attributes.get('CPU_LIST'))

            self.rq_core.deleteFrame(self.runFrame.frameId)

            self.__sendFrameCompleteReport()

            log.info("Monitor frame ended for frameId=%s",
                     self.runFrame.frameId)

class RqCore:
    """Main body of RQD, handles the integration of all components,
       the setup and launching of a frame and acts on all ice calls
       that are passed from the Network module."""
    def __init__(self, opt_nimbyoff=False):
        """RqCore class initialization"""
        self.__whenIdle = False
        self.__respawn = False
        self.__reboot = False

        self.__opt_nimbyoff = opt_nimbyoff

        self.cores = RqdIce.CoreDetail()

        self.nimby = Nimby(self)

        self.machine = Machine(self, self.cores)

        self.network = Network(self)
        self.__thread_lock = threading.Lock()
        self.__cache = { }

        self.shutdownThread = None
        self.updateRssThread = None
        self.onIntervalThread = None

        self.__cluster = None
        self.__session = None
        self.__stmt = None

    def start(self):
        """Called by main to start the rqd service"""
        # If nimby should be on, start it
        if rqconstants.OVERRIDE_NIMBY == True:
            log.warning("Nimby startup has been triggered by OVERRIDE_NIMBY")
            self.nimbyOn()
        elif self.machine.isDesktop():
            if self.__opt_nimbyoff:
                log.warning("Nimby startup has been disabled via --nimbyoff")
            elif rqconstants.OVERRIDE_NIMBY == False:
                log.warning("Nimby startup has been disabled via OVERRIDE_NIMBY")
            else:
                self.nimbyOn()
        # Start ice connection
        self.network.start()

    def iceConnected(self):
        """After ICE connects to the cuebot, this function is called"""
        self.network.reportRqdStartup(self.machine.getBootReport())

        self.updateRssThread = threading.Timer(rqconstants.RSS_UPDATE_INTERVAL, self.updateRss)
        self.updateRssThread.start()

        self.onIntervalThread = threading.Timer(rqconstants.RQD_PING_INTERVAL,
                                                self.onInterval)
        self.onIntervalThread.start()

    def onInterval(self):
        """This is called by self.iceConnected as a timer thread to execute
           every interval"""
        try:
            self.onIntervalThread = threading.Timer(rqconstants.RQD_PING_INTERVAL,
                                                    self.onInterval)
            self.onIntervalThread.start()
        except Exception as e:
            log.critical('Unable to schedule a ping due to {0} at {1}'.format(e, traceback.extract_tb(sys.exc_info()[2])))

        try:
            if self.__whenIdle and not self.__cache:
                if not self.machine.isUserLoggedIn():
                    self.shutdownRqdNow()
                else:
                    log.warning('Shutdown requested but a user is logged in.')

        except Exception as e:
            log.warning('Unable to shutdown due to {0} at {1}'.format(e, traceback.extract_tb(sys.exc_info()[2])))

        try:
            self.sendStatusReport()
        except Exception as e:
            log.critical('Unable to send status report due to {0} at {1}'.format(e, traceback.extract_tb(sys.exc_info()[2])))

    def wait(self):
        """Waits on network.waitForShutdown()"""
        self.network.waitForShutdown()

    def updateRss(self):
        """Triggers and schedules the updating of rss information"""
        if self.__cache:
            try:
                self.machine.rss_update(self.__cache)
            finally:
                self.updateRssThread = threading.Timer(rqconstants.RSS_UPDATE_INTERVAL, self.updateRss)
                self.updateRssThread.start()

    def getFrame(self, frameId):
        """Gets a frame from the cache based on frameId
        @type  frameId: string
        @param frameId: A frame's unique Id
        @rtype:  RunningFrame
        @return: RunningFrame object"""
        return self.__cache[frameId]

    def getFrameKeys(self):
        """Gets a list of all keys from the cache
        @rtype:  list
        @return: List of all frameIds running on host"""
        return self.__cache.keys()

    def storeFrame(self, frameId, runningFrame):
        """Stores a frame in the cache and adds the network adapter
        @type  frameId: string
        @param frameId: A frame's unique Id
        @type  runningFrame: RunningFrame
        @param runningFrame: RunningFrame object"""
        self.__thread_lock.acquire()
        try:
            if self.__cache.has_key(frameId):
                raise RqdIce.RqdIceException("frameId " + frameId +
                                          " is already running on this machine")
            self.__cache[frameId] = runningFrame
        finally:
            self.__thread_lock.release()

        # Add servant to Ice Object Adapter
        self.network.add(runningFrame)

    def deleteFrame(self, frameId):
        """Deletes a frame from the cache
        @type  frameId: string
        @param frameId: A frame's unique Id"""
        self.__thread_lock.acquire()
        try:
            if self.__cache.has_key(frameId):
                # Remove servant from Ice Object Adapter
                iceId = self.__cache[frameId].getIceId()
                self.network.remove(iceId)
                del self.__cache[frameId]
        finally:
            self.__thread_lock.release()

    def killAllFrame(self, reason):
        """Will execute .kill() on every frame in cache until no frames remain
        @type  reason: string
        @param reason: Reason for requesting all frames to be killed"""

        if self.__cache:
            log.warning("killAllFrame called due to: %s\n%s" % (reason, ",".join(self.getFrameKeys())))

        while self.__cache:
            if reason.startswith("NIMBY"):
                # Since this is a nimby kill, ignore any frames that are ignoreNimby
                frameKeys = [frame.frameId for frame in self.__cache.values() if not frame.ignoreNimby]
            else:
                frameKeys = self.__cache.keys()

            if not frameKeys:
                # No frames left to kill
                return

            for frameKey in frameKeys:
                try:
                    self.__cache[frameKey].kill(reason)
                except KeyError:
                    pass
            time.sleep(1)

    def releaseCores(self, reqRelease, releaseHT=None):
        """The requested number of cores are released
        @type  reqRelease: int
        @param reqRelease: Number of cores to release, 100 = 1 physical core"""
        self.__thread_lock.acquire()
        try:
            self.cores.bookedCores -= reqRelease
            maxRelease = self.cores.totalCores \
                        - self.cores.lockedCores \
                        - self.cores.idleCores \
                        - self.cores.bookedCores

            if maxRelease > 0:
                self.cores.idleCores += min(maxRelease, reqRelease)

            if releaseHT:
                self.machine.releaseHT(releaseHT)

        finally:
            self.__thread_lock.release()

        if self.cores.idleCores > self.cores.totalCores:
            log.critical("idleCores have become greater than totalCores: "
                         "%d: %s at %s" % (sys.exc_info()[0],
                                       traceback.extract_tb(sys.exc_info()[2])))

    def respawn_rqd(self):
        """Restarts RQD"""
        os.system("/etc/init.d/rqd3 restart")

    def shutdown(self):
        """Shuts down all rqd systems,
           will call respawn or reboot if requested"""
        self.nimbyOff()
        if self.onIntervalThread is not None:
            self.onIntervalThread.cancel()
        if self.updateRssThread is not None:
            self.updateRssThread.cancel()
        self.network.shutdown()
        self.network.waitForShutdown()
        if self.__respawn:
            log.warning("Respawning RQD by request")
            self.respawn_rqd()
        elif self.__reboot:
            log.warning("Rebooting machine by request")
            self.machine.reboot()
        else:
            log.warning("Shutting down RQD by request")

    #These functions are defined in slice/rqd_ice.ice
    #and called by the cuebot via ICE

    def launchFrame(self, runFrame):
        """This will setup for the launch the frame specified in the arguments.
        If a problem is encountered, a CueExecption will be thrown.
        @type   runFrame: RunFrame
        @param  runFrame: Struct from cuebot"""
        log.info("Running command %s for %s" % (runFrame.command,
                                                runFrame.frameId))
        log.debug(runFrame)

        #
        # Check for reasions to abort launch
        #

        if self.machine.state != HardwareState.Up:
            err = "Not launching, rqd HardwareState is not Up"
            log.info(err)
            raise RqdIce.CoreReservationFailureException(err)

        if self.__whenIdle:
            err = "Not launching, rqd is waiting for idle to shutdown"
            log.info(err)
            raise RqdIce.CoreReservationFailureException(err)

        if self.nimby.locked and not runFrame.ignoreNimby:
            err = "Not launching, rqd is lockNimby"
            log.info(err)
            raise RqdIce.CoreReservationFailureException(err)

        if self.__cache.has_key(runFrame.frameId):
            err = "Not launching, frame is already running on this proc %s" % \
                                                                runFrame.frameId
            log.critical(err)
            raise RqdIce.DuplicateFrameViolationException(err)

        if runFrame.uid <= 0:
            err = "Not launching, will not run frame as uid=%d" % runFrame.uid
            log.warning(err)
            raise RqdIce.InvalidUserException(err)

        if runFrame.numCores <= 0:
            err = "Not launching, numCores must be > 0"
            log.warning(err)
            raise RqdIce.CoreReservationFailureException(err)

        # See if all requested cores are available
        self.__thread_lock.acquire()
        try:
            if self.cores.idleCores < runFrame.numCores:
                err = "Not launching, insufficient idle cores"
                log.critical(err)
                raise RqdIce.CoreReservationFailureException(err)

            if runFrame.environment.get('CUE_THREADABLE') == '1':
                reserveHT = self.machine.reserveHT(runFrame.numCores)
                if reserveHT:
                    runFrame.attributes['CPU_LIST'] = reserveHT

            # They must be available at this point, reserve them
            self.cores.idleCores -= runFrame.numCores
            self.cores.bookedCores += runFrame.numCores
        finally:
            self.__thread_lock.release()

        # Create ICE servant for frame
        frameInfo = RunningFrame(self, runFrame)

        frameInfo.frameAttendantThread = FrameAttendantThread(self, runFrame,
                                                              frameInfo)

        #frameInfo.frameAttendantThread.run()   # Monitor inline
        frameInfo.frameAttendantThread.start()

    def getRunningFrame(self, frameId):
        """Replies with the proxy for the given frameId"""
        try:
            return self.__cache[frameId].getProxy()
        except:
            raise RqdIce.RqdIceException("frameId %s is not running on this"
                                         "machine" % frameId)

    def reportStatus(self, current = None):
        """Replies with hostReport"""
        return self.machine.getHostReport()

    def shutdownRqdNow(self):
        """Kill all running frames and shutdown RQD"""
        self.machine.state = HardwareState.Down
        self.lockAll()
        self.killAllFrame("shutdownRqdNow Command")
        if not self.__cache and self.shutdownThread is None:
            self.shutdownThread = threading.Timer(1, self.shutdown)
            self.shutdownThread.start()

    def shutdownRqdIdle(self):
        """When machine is idle, shutdown RQD"""
        self.lockAll()
        self.__whenIdle = True
        self.sendStatusReport()
        if not self.__cache:
            self.shutdownRqdNow()

    def restartRqdNow(self):
        """Kill all running frames and restart RQD"""
        self.__respawn = True
        self.shutdownRqdNow()

    def restartRqdIdle(self):
        """When machine is idle, restart RQD"""
        self.lockAll()
        self.__whenIdle = True
        self.__respawn = True
        self.sendStatusReport()
        if not self.__cache:
            self.shutdownRqdNow()

    def rebootNow(self):
        """Kill all running frames and reboot machine.
           This is not available when a user is logged in"""
        log.warning('Requested to reboot now')
        if self.machine.isUserLoggedIn():
            err = "Rebooting via RQD is not support for a desktop machine when a user is logged in"
            log.warning(err)
            raise RqdIce.RqdIceException(err)
        self.__reboot = True
        self.shutdownRqdNow()

    def rebootIdle(self):
        """When machine is idle, reboot it"""
        log.warning('Requested to reboot machine when idle')
        self.lockAll()
        self.__whenIdle = True
        self.__reboot = True
        self.sendStatusReport()
        if not self.__cache and not self.machine.isUserLoggedIn():
            self.shutdownRqdNow()

    def nimbyOn(self):
        """Activates nimby, does not kill any running frames until next nimby
           event. Also does not unlock until sufficent idle time is reached."""
        if os.getuid() != 0:
            log.warning("Not starting nimby, not running as root")
            return
        if not self.nimby.active and platform.system() == "Linux":
            try:
                self.nimby.start()
                log.info("Nimby has been activated")
            except:
                self.nimby.locked = False
                err = "Nimby is in the process of shutting down"
                log.warning(err)
                raise RqdIce.RqdIceException(err)

    def nimbyOff(self):
        """Deactivates nimby and unlocks any nimby lock"""
        if self.nimby.active:
            self.nimby.stop()
            log.info("Nimby has been deactivated")

    def onNimbyLock(self):
        """This is called by nimby when it locks the machine.
           All running frames are killed.
           A new report is sent to the cuebot."""
        self.__reportNimbyChange(True)
        self.killAllFrame("NIMBY Triggered")
        self.sendStatusReport()

    def onNimbyUnlock(self, asOf=None):
        """This is called by nimby when it unlocks the machine due to sufficent
           idle. A new report is sent to the cuebot.
        @param asOf: Time when idle state began, if known."""
        self.__reportNimbyChange(False, asOf=asOf)
        self.sendStatusReport()

    def lock(self, reqLock):
        """Locks the requested core.
        If a locked status changes, a status report is sent to the cuebot.
        @type  reqLock: int
        @param reqLock: Number of cores to lock, 100 = 1 physical core"""
        send_update = False
        self.__thread_lock.acquire()
        try:
            numLock = min(self.cores.totalCores - self.cores.lockedCores,
                          reqLock)
            if numLock > 0:
                self.cores.lockedCores += numLock
                self.cores.idleCores -= min(numLock, self.cores.idleCores)
                send_update = True
        finally:
            self.__thread_lock.release()

        log.debug(self.cores)

        if send_update:
            self.sendStatusReport()

    def lockAll(self):
        """"Locks all cores on the machine.
            If a locked status changes, a status report is sent."""
        send_update = False
        self.__thread_lock.acquire()
        try:
            if self.cores.lockedCores < self.cores.totalCores:
                self.cores.lockedCores = self.cores.totalCores
                self.cores.idleCores = 0
                send_update = True
        finally:
            self.__thread_lock.release()

        log.debug(self.cores)

        if send_update:
            self.sendStatusReport()

    def unlock(self, reqUnlock):
        """Unlocks the requested number of cores.
        Also resets reboot/shutdown/restart when idle requests.
        If a locked status changes, a status report is sent to the cuebot.
        @type  reqUnlock: int
        @param reqUnlock: Number of cores to unlock, 100 = 1 physical core"""

        send_update = False

        if self.__whenIdle or self.__reboot or self.__respawn or \
           self.machine.state != HardwareState.Up:
            send_update = True

        self.__whenIdle = False
        self.__reboot = False
        self.__respawn = False
        self.machine.state = HardwareState.Up

        self.__thread_lock.acquire()
        try:
            numUnlock = min(self.cores.lockedCores, reqUnlock)
            if numUnlock > 0:
                self.cores.lockedCores -= numUnlock
                self.cores.idleCores += numUnlock
                send_update = True
        finally:
            self.__thread_lock.release()

        log.debug(self.cores)

        if send_update:
            self.sendStatusReport()

    def unlockAll(self):
        """"Unlocks all cores on the machine.
            Also resets reboot/shutdown/restart when idle requests.
            If a locked status changes, a status report is sent."""

        send_update = False

        if self.__whenIdle or self.__reboot or self.__respawn or \
           self.machine.state != HardwareState.Up:
            send_update = True

        self.__whenIdle = False
        self.__reboot = False
        self.__respawn = False
        self.machine.state = HardwareState.Up

        self.__thread_lock.acquire()
        try:
            if self.cores.lockedCores > 0:
                if not self.nimby.locked:
                    self.cores.idleCores += self.cores.lockedCores
                self.cores.lockedCores = 0
                send_update = True
        finally:
            self.__thread_lock.release()

        log.debug(self.cores)

        if send_update:
            self.sendStatusReport()

    def sendStatusReport(self):
        self.network.reportStatus(self.machine.getHostReport())

    def __reportNimbyChange(self, locked, asOf=None):
        try:
            from cassandra.cluster import Cluster
            from cassandra.util import datetime_from_timestamp
            from cassandra.util import uuid_from_time

            if self.__cluster is None:
                self.__cluster = Cluster(['eat-cassa01', 'eat-cassa03', 'eat-cassa05'])
                self.__session = self.__cluster.connect('cue3')

                # Expire entries after 6 months.
                ttl = 86400 * 364 // 2

                self.__stmt = self.__session.prepare(
                    """INSERT INTO nimby (day, hostname, ts, locked, active)
                       VALUES (?, ?, ?, ?, ?)
                       USING TTL {0}""".format(ttl))

            if asOf is None:
                asOf = time.time()

            day = datetime_from_timestamp(asOf)
            day = day.replace(hour=0, minute=0, second=0, microsecond=0)

            # Fire and forget!
            self.__session.execute_async(
                self.__stmt.bind((
                    day,
                    self.machine.getHostname(),
                    uuid_from_time(asOf),
                    locked,
                    self.nimby.active)))

        except Exception as e:
            log.warning("Failed to report nimby change: {0}".format(str(e)))
