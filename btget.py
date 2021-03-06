#!/usr/bin/env python

# usage:
#   btget foo.torrent [-rpc-port=x] [-peer-port=y] [-dir=d] [-stdout] [-verbose]
# note:
#   port settings are advisory, if daemon cannot start, other ports will be found

# REQUIRES:
#  transmission-daemon and transmission-remote

# SUBPROCESSES:
#  transmission-daemon 
#       listening for rpc on RPC_PORT (default 9091)
#       taking peer requests on PEER_PORT (default 51413)

# NOTES:
#  hard-coded to only try range 9091 and following 25 ports for RPC
#  tempdir might need write perms so daemon (debian-transmission) can write to it
#   ...but since daemon now runs in our process group, it could inherit them
#  transmissionCredentials are currently hardcoded

# TODO:
#  Add mutable transmission-remote rpc authentication credentials and port 
#  How to handle UTF chars in torrent filenames?
#  State machine still brittle when trying to start a torrent already loaded in transmission-d

#  Add seed/leech ratio management so we maintain at least parity

import io
import os
import sys
import shutil
import string
import time
import httplib
from urlparse import urlparse 
import urllib2
import subprocess
import datetime
import codecs
import re
import pprint
import urllib
import hashlib
import StringIO
import random
import signal

sys.path.append("/petabox/sw/lib/python")
# sys.path.append("/home/ximm/projects/bitty/")   # only needed until module pi'd to workers
import bencode      # in /petabox/sw/lib/python/

try:
    import psyco # Optional, 2.5x improvement in speed
    psyco.full()
except ImportError:
    pass

decimal_match = re.compile('\d')

# decoding support

def version():
    return '$Revision: 37286 $ $Date: 2011-07-26 01:12:06 +0000 (Tue, 26 Jul 2011) $'

def bdecode(data):
    '''Main function to decode bencoded data'''
    chunks = list(data)
    chunks.reverse()
    root = _dechunk(chunks)
    return root

def _dechunk(chunks):
    item = chunks.pop()

    if item == 'd': 
        item = chunks.pop()
        hash = {}
        while item != 'e':
            chunks.append(item)
            key = _dechunk(chunks)
            hash[key] = _dechunk(chunks)
            item = chunks.pop()
        return hash
    elif item == 'l':
        item = chunks.pop()
        list = []
        while item != 'e':
            chunks.append(item)
            list.append(_dechunk(chunks))
            item = chunks.pop()
        return list
    elif item == 'i':
        item = chunks.pop()
        num = ''
        while item != 'e':
            num  += item
            item = chunks.pop()
        return int(num)
    elif decimal_match.search(item):
        num = ''
        while decimal_match.search(item):
            num += item
            item = chunks.pop()
        line = ''
        for i in range(int(num)):
            line += chunks.pop()
        return line

# logging   
            
def dlog ( lev, str ):
    global sout
    global dlogfile
    global dloglevel
    if lev <= dloglevel and dlogfile is not None:
        lt = datetime.datetime.now()
        try:
            dlogfile.write( "%s %s\n" %  ( lt,  str.encode( 'utf-8' ) ) )
        except:
            try:
                dlogfile.write ( "%s <* removed unprintable chars *> %s\n" % (lt, printable( str ) ) )
            except:
                dlogfile.write ( "%s <* unprintable *>\n" % lt )
        if sout is True:
            print str
            sys.stdout.flush()
        dlogfile.flush()
    return

def dlogAppend ( lev, str ):
    global sout
    global dlogfile
    global dloglevel
    if lev <= dloglevel and dlogfile is not None:
        try:
            dlogfile.write( "%s" %  ( str.encode( 'utf-8' ) ) )
        except:
            try:
                dlogfile.write ( "%s <* removed unprintable chars *> %s" % (lt, printable( str ) ) )
            except:
                dlogfile.write( "%s <* unprintable *>\n" % lt )
        if sout is True:
            print str,
            sys.stdout.flush()
        dlogfile.flush()
    return


def printable ( dirty ):
    # eliminate non-printable chars
    clean = "".join(i for i in dirty if ord(i) < 128)
#    clean = ''.join([char for char in dirty if isascii(char)])
#    return ''.join([char for char in clean if isprint(char)])
    return clean

# core
    
def parseTorrent( torrentFile ):
    """Return a tuple with torrent name, info hash, torrent files, and printable info for logging"""
    try: 
        with open( torrentFile, "rb") as f:
            metainfo = bdecode( f.read() )
                
        info = metainfo['info']

        if 'files' in info:
            fils = info['files']
            # screen file names for malicious/corrupt content
            # very basic screening: currently look for ..
            # not checking for inclusion of .., 'this was too long....mp3' is legal
            for aFile in fils:
                fp = aFile[ 'path' ]
                for aNode in fp:
                    if aNode == '..' or '/' in aNode:
                        raise ValueError
        else:
            # special handling for case of single-file torrents
            # construct data strutures to mimic standard multi-file case
            oneFile = {}
            oneFile[ 'path' ]= [ info[ 'name' ] ]
            oneFile[ 'length' ]= info[ 'length' ]
            fils = [ oneFile ]
        
        encodedInfo = bencode.bencode(info)
        infoHash = hashlib.sha1(encodedInfo).hexdigest() # .upper()
    
        tmpStream = StringIO.StringIO()        
    
        pieces = StringIO.StringIO(info['pieces'])
        hashes = info['pieces'] 
        numhashes = len(hashes) / 20    
        info['pieces'] = ' -- hashes -- '   
    
        pprint.pprint ( metainfo, stream=tmpStream, indent=4 )
    
        torrentInfo = '%s\nSHA1 info_hash: %s' % (tmpStream.getvalue(), infoHash )

        torrentName = info['name']
        # a subdirectory with this name automatically created by transmission (by default)
        # so do a cursory check for malice
        if torrentName == '..' or '/' in torrentName:
            raise ValueError        

        return ( torrentName, infoHash, fils, torrentInfo )

    except:
        return None        



def retrieveTorrent ( torrentPath, torrentFile, torrentName, infoHash, filelist, torrentDir ):
    """Use an instance of transmission-daemon via transmission-remote to retrieve one .torrent"""

    dlog (1, 'Retrieving %s into %s' % ( torrentPath, torrentDir) )
    
    ( daemonShellProcess, daemonPID ) = getDaemon()
    if daemonShellProcess == None:
        dlog (1, 'FAILED: could not start daemon' )
        return 1

    startTorrent = True

    # check whether we've retrieved/started this torrent already
    comCode, res, err = transmissionTorrentState ( infoHash )
    state = findState ( res )
    if state == 'Idle':
        dlog (1, 'NOSTART: torrent already downloaded' )
        startTorrent = False
    elif state == 'Downloading' or state == 'Up & Down':
        dlog (1, 'NOSTART: torrent downloading now' )
        startTorrent = False
    elif state == 'Stopped':
        dlog (1, 'RESTARTING: torrent stopped, attempting to restart' )
    elif state != '(None)':
        dlog (1, 'FAILED: torrent exists in unhandled state ( %s )' % state )
        return 1         
    
    if startTorrent is True:    
        # set the directory in which the torrent will download
        # prevent transmission-d from caching in a working dir, in case we fail
        # note that each torrent goes into its own subdirectory
        comlist = [ '--no-incomplete-dir',
                    '--download-dir',
                    torrentDir ]
        ret = transmissionCommand (comlist)
        dlog(2, 'transmission-remote... %s' % ( ' '.join( comlist ) ) )
                
        # torrent path/fn provided must be fully qualified if not in ~
        comlist = ['--add', torrentPath ]
        comCode, res, err = transmissionCommand (comlist)
        resp = findResp ( res )
        dlog(2, 'transmission-remote... %s' % ( ' '.join( comlist ) ) )
        dlog(2, 'Response: %s' % resp )
        if resp != '"success"' and resp != '"duplicate torrent"':
            dlog (1, 'FAILED: could not load %s, exitcode %s\n%s\n%s' % (torrentPath, comCode, res, err ) )
            return 1       

        # write the infohash for the file (Torrent.php will use to add metadata)
        hashFile = '%s/%shash' % ( torrentDir, torrentFile ) 
        with codecs.open( hashFile, encoding='utf-8', mode="w" ) as cfile:
            cfile.write('%s\n%s\n' % ( infoHash, torrentName ) )
              
        dlsize = 0
        for aFile in filelist:
            fs = aFile['length']
            dlsize = dlsize + int(fs)

        dlsizeK = dlsize / 1024.0
        dlsizeMB = dlsizeK / 1024.0
        dlog (1, 'Downloading %s MB' % round( dlsizeMB, 2) )    

    # wait for completion...
    # TODO build a better state machine
    
    finished = False
    success = False
    mins = 0
    maxUp = 0.0
    maxDown = 0.0
    maxUpPeers = 0
    maxDownPeers = 0
    maxPeers = 0

    while finished is False:
        # clear out buffers for the daemonShellProcess
        try:
            bitbucket = daemonShellProcess.stdout.flush()
        except:
            bitbucket = None
        if giveUpP ( infoHash, mins ) is True:
            dlog( 1, '\nDownload abandoned after %s minutes.' % mins )
            finished = True
        comCode, res, err = transmissionTorrentState ( infoHash )
        state = findState( res )
        perc = findVal( 'Percent Done: ', res )
        if state == 'Idle' or state == 'Seeding':
            if perc == '100%':
                dlog( 1, 'Download completed' )
                finished = True
                success = True                    
        elif state == 'Stopped':
            error = findError( res )
            dlog( 1, 'Download stopped at %s complete' % perc )
            if perc == '100%':
                success = True
            if error != 'None':
                dlog( 1, 'ERROR: %s' % error )                
            finished = True
        elif state == '(None)':
            dlog( 1, 'Torrent missing, may have been manually removed (or daemon killed)' )
            finished = True            
        # wait a bit
        # fugly logging logic... sorry <ducks>
        if finished == False:
            logstr = 'Percent Done: %s' % perc
            peerinfo = findVal( 'Peers: ', res ).split(',')
            if len( peerinfo ) == 3:
                cstr, ustr, dstr = peerinfo
                ds = findVal( 'Download Speed: ', res )
                us = findVal( 'Upload Speed: ', res )
                rat = findVal( 'Ratio: ', res )
                cp = cstr.split( 'connected to ' )[-1]
                up = ustr.split( 'uploading to ' )[-1]
                dp = dstr.split( 'downloading from ' )[-1]
                logstr = '%s Peers: ^ %s to %s, v %s from %s, of %s (Ratio: %s)' % (logstr,us,up,ds,dp,cp,rat)
                maxUp = max( maxUp, float(us.split()[0] ) )
                maxDown = max( maxDown, float(ds.split()[0] ) )
                maxUpPeers = max( maxUpPeers, int(up.split()[0] ) )
                maxDownPeers = max( maxDownPeers, int(dp.split()[0] ) )
                maxPeers = max( maxPeers, int(cp.split()[0] ) )
            time.sleep(60)
            mins = mins + 1
            if mins < 15:
                dlog (1, '.     %s' % logstr )
            else:
                if mins < 60 and mins % 5 == 0:
                    dlog (1, '..    %s' % logstr )            
                else:
                    if mins < (60 * 24) and mins % 60 == 0:
                        dlog (1, '...   %s' % logstr )    
                    else:
                        if mins % (60 * 24) == 0:
                            dlog (1, '....  %s' % logstr )

    dlog (1, 'PEAK SPEED (PEERS): ^ %s (%s), v %s (%s); PEAK PEERS: %s' % (maxUp,maxUpPeers,maxDown,maxDownPeers,maxPeers) )

    # TODO: maintain seeding ratio by keeping alive until ratio reached
    #  issue: what if no one is interested...? :P  

    # remove the torrent from seeding/download list
    comlist = ['--torrent', infoHash, '--remove' ]
    comCode, res, err = transmissionCommand (comlist)
    dlog (1, 'Removing torrent from daemon if necessary...')

    dlog (1, 'Terminating daemon...')        
    os.kill( daemonShellProcess.pid, signal.SIGTERM )
    if daemonPID != 0:
        os.kill( daemonPID, signal.SIGTERM )
    daemonShellProcess = None
    
    if success is True:
        return 0    
    else:
        return 1


def getDaemon():
    """Try to find an open RPC and PEER port pairing and instantiate a daemon on them
    Return (None, 0) if no daemon created, or, ( shellSubprocess, PID ) to kill after torrent retrieval
    Two kills are required: one for the shell, one for the daemon"""
    global RPC_PORT
    global PEER_PORT
    global transmissionCredentials

    # maintain a fixed port relationship
    offset = 51413 - 9091
        
    # verify that transmission-d is installed first...(!)
    
    finishedLooking = False

    dlog (1, 'Trying to instantiate a new instance of transmission-daemon on an unused port pair...' )            

    daemonShellProcess = None

    # DEBUG    
    numLeftToTry = 25
    
    while finishedLooking is False:    
        # Launch a daemon with a given set of ports, then scan its verbose debugging output checking for the strings
        # that uniquely indicate that it tried, and failed, to bind to the requested RPC control port RPC_PORT:
        #  [18:43:59.582] Couldn't bind port 52000 on 0.0.0.0: Address already in use (Is another copy of Transmission already running?) (net.c:369)
        # If we reach the succeeding line loading settings, binding was successful:
        #  [18:43:59.582] Using settings from "/home/ximm/.config/transmission-daemon" (daemon.c:425)
        PEER_PORT = RPC_PORT + offset            
        dlog (1, 'Starting daemon with RPC port %s, peer port %s' % ( RPC_PORT, PEER_PORT ) )            
        comstr = "bash -c 'echo DaemonPID: $$; exec transmission-daemon --config-dir /etc/transmission-daemon --port %s --peerport %s --foreground 2>&1'" % ( str( RPC_PORT ), str( PEER_PORT ) )
        dlog (1, 'Command: %s' % comstr )

        # NOTE: uses shell=True so quoting of exec works... sorry, Sam! <ducks>
        # NOTE: do not use preexec_fn=os.setsid in Popen, as the process is then not killed if btget is (or errs)
        daemonShellProcess = subprocess.Popen(   comstr,
                                            shell = True,
                                            stderr = subprocess.STDOUT,
                                            stdout = subprocess.PIPE ) 

        tout = 0
        gotStatus = False
        daemonOK = False
        daemonPID = 0
        while gotStatus is False:
            # TK risk of blocking here in readline()... solutions are tricky
            #  could use pexpect or equivalent; or one of the solutions here:
            #  http://stackoverflow.com/questions/375427/non-blocking-read-on-a-subprocess-pipe-in-python
            aline = daemonShellProcess.stdout.readline().strip()
            dlog (1, '  %s' % aline )
            if 'DaemonPID' in aline:
                daemonPID = int ( aline.partition( 'DaemonPID: ' )[-1] )
            if 'command not found' in aline or 'Permission denied' in aline:
                # problem with transmission-d, abort rather than blocking
                dlog (1, 'FAILED: uhandled exception: %s' % aline )            
                gotStatus = True
            if 'bind' in aline:
                # Assume line is 'Couldn't bind port...' which means port taken
                dlog (1, 'FAILED: port(s) appear taken ( %s seconds )' % tout )            
                gotStatus = True
            elif 'Using settings' in aline:
                # This line always comes after bind attempt; must be success
                dlog (1, 'SUCCESS: port(s) appear free ( %s seconds )' % tout )            
                daemonOK = True
                gotStatus = True
            if tout > 60:
                # 60 seconds, and no reply? Should have gotten SOMETHING. Abort!
                gotStatus = True
            tout = tout + 0.25
            time.sleep(0.25)
        
        if daemonOK is True:
            # apparently we have a usable daemon attached to daemonShellProcess
            # verify we can talk to it using transmission-remote on the expected port
            comlist = ['--session-info' ] 
            comCode, res, err = transmissionCommand ( comlist )
            if res is not None and res !='':
                dlog (1, 'Verified communication with daemon, --session-info returns:\n%s' % res )            
                finishedLooking = True
            elif err is not None and err !='':
                dlog (1, 'Problem communicating with daemon, --session-info returns:\n%s' % err )            

        if finishedLooking is False:        
            dlog (1, 'Terminating unusable daemon...' )
            # two processes to kill: the shell (daemonShellProcess.pid) and the daemon (reported by echo)
            # we could use killpg and setsid, but that forks that group and it is not killed if btget is
            os.kill( daemonShellProcess.pid, signal.SIGTERM )
            if daemonPID != 0:
                os.kill( daemonPID, signal.SIGTERM )
            daemonShellProcess = None

            # try next port
            RPC_PORT = RPC_PORT + 1
            numLeftToTry = numLeftToTry - 1
            if numLeftToTry == 0 or PEER_PORT > 62000:
                dlog (1, 'ABORT: failed to find open ports' )            
                finishedLooking = True
                
    if daemonShellProcess is not None:
        dlog (1, 'Using transmission-daemon on rpc port %s' % RPC_PORT )                    

    return ( daemonShellProcess, daemonPID )
    
    
def writeManifest ( torrentDir, torrentFile, filelist, fixFilenames ):
    """ write the list of retrieved files to .torrentcontents
    since we're walking the files, scan each path and return a dict mapping all path elements to sanitized versions of each """
    repDict = {}
    dirtyPathLists = []
    if '.torrent' in torrentFile:
        basename = torrentFile.split('.torrent')[0]
    else:
        basename = torrentFile
    contFile = '%s/%s_torrent.txt' % ( torrentDir, basename ) 
    with codecs.open( contFile, encoding='utf-8', mode="w" ) as cfile:
        for aFile in filelist:
            # path is array expressing a dir path, last of which is fn
            #  c.f. http://www.bittorrent.org/beps/bep_0003.html
            fsize = aFile['length']
            dirtyPathList = aFile['path']
            dirtyPathLists.append( dirtyPathList )
            dirtyPath = '/'.join( dirtyPathList )
            cleanPathList = []
            for dirtyPart in dirtyPathList:
                cleanPart = sanitizeFilename ( dirtyPart, repDict )
                cleanPathList.append( cleanPart )
            cleanPath = '/'.join( cleanPathList )     
            if fixFilenames is True:
                cfile.write ('%s,%s,%s\n' % (cleanPath, fsize, dirtyPath ) )
            else:
                # if dirty path contains commas (can happen!) parsing will break horribly
                cfile.write ('%s,%s,%s\n' % (dirtyPath, fsize, dirtyPath ) )            
    return (dirtyPathLists, repDict)

def sanitizeFilename ( dirty, repDict ):
    """Return a file or directory name that has no spaces or punctuation, length-limited""" 
    # NOTE: operates on parts, not on a full path!
    # TODO: test for illegal file names on Windows? e.g. COM1 or NUL...
    if dirty in repDict:
        return repDict[ dirty ]
    # this could in theory be done via string.translate()
    lessdirty = dirty.replace(' ','_').replace('[','(').replace(']',')').replace('{','(').replace('}',')')    
    validchars = "-_.() %s%s" % (string.ascii_letters, string.digits)
    validchars = frozenset( validchars )
    clean = ''.join(c for c in lessdirty.strip() if c in validchars)
    if len(clean) == 0:
        # the way split works on paths, it's possible we were passed an empty string
        if len(dirty) != 0:
            return 'generated_filename_%s' % random.randint(1000,9999)
    if len(clean) > 128:
        return '%s%s' % ( clean[0:124], random.randint(1000,9999) )
    repDict[ dirty] = clean
    return clean

def buildRepDict( dirtyPaths ):
    repDict = {}
    for aPath in dirtyPaths:
        if '/' in aPath:        
            pathList = aPath.split( '/' )
            for aPart in pathList[:]:
                sanitizeFilename ( aPart, repDict )
        else:
            sanitizeFilename ( aPath, repDict )
    return repDict

def mvWithDict ( torrentDir, dirtyPathLists, repDict ):
    """Traverse dirtyPaths recursively, renaming every file acccording to repDict"""
    dlog(0, "btget: sanitizing directories and files..." )
    for aDirtyPathList in dirtyPathLists:

        recursivelyRename( torrentDir, aDirtyPathList[:], repDict )

def recursivelyRename( prefixPath, pathList, repDict ):
    """Use mv to rename the top of each directory, then move inward.
    Works since the renamed prefixing path is passed in..."""
    tophere = pathList[0]
    newtop = repDict[ tophere ]
    if tophere != newtop:
        src = '%s%s' % (prefixPath, tophere)
        trg = '%s%s' % (prefixPath, newtop) 
        dlog(1, 'Considering renaming %s as %s' % (src, trg) )    
        if os.access( ('%s%s' % (prefixPath, tophere)), os.F_OK ) is True:
            try:
                os.rename(src,trg)
                dlog(1, 'SUCCESS: renamed!' )
            except OSError as (errno, strerror):
                dlog(1, 'FAILED: (%s) %s' % (errno, strerror ) )   
    #            comlist = ['mv', src, trg ]
    #            dlog(1, ' '.join(comlist) )
    #            (exitcode, res, err) = shellCommand( comlist )
    #            dlog(1, 'exitcode: %s\nerr: %s\n%s' % (exitcode, err, res) )
        else:
            dlog(1, 'SKIPPING: already renamed' )    
    else:
        dlog(1, 'SKIPPING: do not need to rename %s' % (tophere) )
    if len( pathList ) > 1:
        prefixPath = prefixPath + newtop + '/'
        recursivelyRename( prefixPath, pathList[1:], repDict )


# Helpers

def giveUpP ( infoHash, min):
    # TODO: check download seeders here? giveUp can manage its own state...
    # for now just give up after a week... :P
    if min > (60 * 24 * 7):
        return True
    else:
        return False
        
def findState ( stringOfLines ):
    return findVal ( 'State: ', stringOfLines )

def findResp ( stringOfLines):
    global RPC_PORT
    return findVal ( ('localhost:%s responded: ' % RPC_PORT), stringOfLines )

def findError ( stringOfLines ):
    return findVal ( 'Error: ', stringOfLines )
        
def findVal ( keytext, stringOfLines ):
    try:
        # note: attempt to access a result via index -1 will raise exception if not found
        theLine =  [l for l in stringOfLines.splitlines() if (keytext in l)][-1]
        theVal = theLine.split( keytext )[-1].strip()
        return theVal
    except:
        return '(None)'

def transmissionTorrentState ( infoHash ):
    comlist = [ '--torrent', infoHash, '--info' ]
    return transmissionCommand ( comlist )
        
def transmissionCommand ( comlist ):
    global RPC_PORT
    return transmissionCommandOnPort ( comlist, RPC_PORT )
    
def transmissionCommandOnPort ( comlist, rpcport ):
    global transmissionCredentials
    ourlist = [ 'transmission-remote',
                str( rpcport ),
                '--auth',
                transmissionCredentials ]

    ourlist.extend( comlist )
    return shellCommand ( ourlist )

def shellCommand ( comlist ):
    global transmissionCredentials
    # abominable hack to hide credentials in log :P
    safelist = comlist[:]
    if transmissionCredentials in safelist:
        idx = safelist.index( transmissionCredentials )
        safelist[idx] = 'xxxxxxx:xxxxxxx'
    comstring = ' '.join( safelist )
    dlog( 3, comstring )
    p = subprocess.Popen( comlist, stderr = subprocess.STDOUT, stdout = subprocess.PIPE )
    (res, err) = p.communicate()
    exitcode = p.wait()
    ret = (exitcode, res, err)
    return ret
        
def tempDirForTorrent( infoHash ):
    global TEMP_DIR
    
    torrentDir = '%s/%s/' % (TEMP_DIR, infoHash )
    if os.access( torrentDir, os.F_OK ) is False:
        dlog( 2, 'Making item directory %s' % torrentDir )
        os.mkdir( torrentDir )
    else:
        dlog( 2, 'Found existing item directory %s' % torrentDir )
    return torrentDir


    
# Remember the Main



def main(argv=None):

    global verbose
    global dlogfile         # [verbose] logging for btget
    global retlogfile       # retry log in CSV form
    global dloglevel        # for our own logging only; 1 = terse, 2 = verbose, 3 = debugging

    global sout             # dlog prints to standard out as well

    global TEMP_DIR
    global RPC_PORT
    global PEER_PORT
    
    global transmissionCredentials
        
    sout = False    
            
    if argv is None:
        argv = sys.argv

    torrentFile = ''
    
    dryrun = False
    verbose = False
    debug = False
    fixFilenames = False

    makeTempDir = True
    TEMP_DIR = '/tmp/' 

    RPC_PORT = 9091
    PEER_PORT = 51413
    
    dlfn = None    
        
    for anArg in argv:
        if anArg[0] is "-":
            qual = anArg[1:len(anArg)]
            if qual == "dry":
                dryrun = True
            elif qual == "debug":
                debug = True
            elif qual == "verbose":
                verbose = True
            elif qual == "stdout":
                sout = True
            elif qual == "sanitize":
                fixFilenames = True
            elif "rpc-port=" in qual:
                RPC_PORT = qual.split("rpc-port=")[-1]
            elif "peer-port=" in qual:
                PEER_PORT = qual.split("peer-port=")[-1]
            elif "dir=" in qual:
                makeTempDir = False
                TEMP_DIR = qual.split("dir=")[-1]
            elif "log=" in qual:
                dlfn = qual.split("log=")[-1]
        else:
            torrentPath = anArg
            if '/' in torrentPath:
                torrentFile = torrentPath.split('/')[-1]
            else:                
                torrentFile = torrentPath
    
    if TEMP_DIR[-1] != "/":
        TEMP_DIR = TEMP_DIR + "/"                    

    if dlfn is None:
        dlfn = TEMP_DIR + torrentFile + '.log'
    
    if verbose is True:
        dloglevel = 2
    else:
        dloglevel = 1
    
    if debug is True:
        dlogLevel = 3

    transmissionCredentials = 'archive:BigData300'
    
    with codecs.open( dlfn, encoding='utf-8', mode="a" ) as dlogfile:
        if torrentFile is None:
            dlog(0, 'btget: (2) no torrent file specified, aborting')
            print 'btget: (2) no torrent file specified, aborting'
            print 'Usage: btget torrentfile [-verbose] [-dir=destination] [-log=logfile] [-verbose] [-stdout] [-sanitize]'  
            return 2
        tup = parseTorrent ( torrentFile ) # returns none on Fails        
        if tup is None:
            dlog(0, "btget: (1) problem with torrent file %s" % torrentPath)
            return 1
        else:
            torrentName, infoHash, filelist, torrentInfo = tup
            
            if makeTempDir is True:
                # default to store torrent in TEMP_DIR/infoHash/ 
                if os.access( TEMP_DIR, os.F_OK ) is False:
                    os.mkdir( TEMP_DIR ) 
                torrentDir = tempDirForTorrent( infoHash )                   
            else:
                # unless one is passed in
                if os.access( TEMP_DIR, os.F_OK ) is False:
                    dlog(0, "btget: (1) directory does not exist %s" % TEMP_DIR)
                    return 1
                torrentDir = TEMP_DIR

            dlog(2, "Torrent parse:\n%s" % torrentInfo )
            
            res = retrieveTorrent ( torrentPath, torrentFile, torrentName, infoHash, filelist, torrentDir )
            if res == 0:                
                (dirtyPathLists, repDict) = writeManifest ( torrentDir, torrentFile, filelist, fixFilenames )
                if fixFilenames is True:
                    # by default transmission stores torrents in the subdir suggested by the name field (per bittorrent convention)
                    saveDir = '%s%s/' % (torrentDir, torrentName)
                    mvWithDict ( saveDir, dirtyPathLists, repDict )
                dlog(0, "btget: (0) retrieved %s" % torrentFile)
                return 0
            elif res == 2:
                dlog(0, "btget: (1) problem with torrent file %s" % torrentFile)
                return 1
            elif res == 1:
                dlog(0, "btget: (1) failed trying to retrieve %s" % torrentFile)
                return 1
            else:
                dlog(0, "btget: (1) unknown failure retrieving %s" % torrentFile)
                return 1
                        
if __name__ == "__main__":
    sys.exit(main())
