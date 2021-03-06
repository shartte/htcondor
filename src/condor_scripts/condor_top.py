#!/usr/bin/env python

import htcondor
import classad

import sys
import os
import re
import datetime
import optparse

from operator import itemgetter
from time import sleep
from copy import deepcopy

### command-line options ###
parser = optparse.OptionParser()

# daemons
parser.set_defaults(daemon='Collector')
group = optparse.OptionGroup(parser, title='Daemon Options', description=(
    'Monitor ClassAds from the specified daemon. If none of these options are '
    'used, condor_collector will be monitored.'))
group.add_option('--collector', action='store_const', const='Collector',
                     dest='daemon', help=(
                         'Monitor condor_collector ClassAds. '
                         'If -n is not set, COLLECTOR_HOST will be queried.'))
group.add_option('--master', action='store_const', const='Master',
                     dest='daemon', help=(
                         'Monitor condor_master ClassAds. '
                         'If -n is not set, COLLECTOR_HOST will be queried.'))
group.add_option('--negotiator', action='store_const', const='Negotiator',
                     dest='daemon', help=(
                         'Monitor condor_negotiator ClassAds. '
                         'If -n is not set, COLLECTOR_HOST will be queried.'))
group.add_option('--schedd', action='store_const', const='Schedd',
                     dest='daemon', help=(
                         'Monitor condor_schedd ClassAds. '
                         'If -n is not set, FULL_HOSTNAME will be queried.'))
group.add_option('--startd', action='store_const', const='Startd',
                     dest='daemon', help=(
                         'Monitor condor_startd ClassAds. If -n is not set, '
                         'slot1@FULL_HOSTNAME will be queried.'))
parser.add_option_group(group)

# pool
parser.add_option('-p', '--pool', dest='pool',
                      default=htcondor.param['COLLECTOR_HOST'], help=(
    'Query the daemon via the specified central manager. If omitted, the value '
    'of the configuration variable COLLECTOR_HOST is used.'))

# name
parser.add_option('-n', '--name', dest='name', help=(
    'Query the daemon named NAME. If omitted, the value used will depend on '
    'the type of daemon queried (see Daemon Options). If the named daemon '
    'returns multiple ClassAds, the first ClassAd returned will be monitored.'))

# delay
parser.add_option('-d', '--delay', type='int', dest='delay',
                      default=htcondor.param['STATISTICS_WINDOW_QUANTUM'],
                      help=(
    'Specifies the delay between ClassAd updates, in integer seconds. '
    'If omitted, the value of the configuration variable '
    'STATISTICS_WINDOW_QUANTUM is used.'))

# sort column
parser.add_option('-s', '--sort', dest='sort_column', default='Runtime', help=(
    'Sort table by column. Available columns: '
    'Runtime (default), InstAvg, Avg, CountRate, Count, Max, Item'))

# additional ClassAd attributes
parser.add_option('--attrs', dest='attrs', default='', help=(
    'Comma-delimited list of additional ClassAd attributes to monitor.'))

# set global variables based on command-line options
opts, args = parser.parse_args()
POOL = opts.pool
DAEMON = opts.daemon
NAME = opts.name
ATTRIBUTES = [attr.strip() for attr in opts.attrs.split(',')]
DELAY = opts.delay
SORT = opts.sort_column

if not NAME: # make sure NAME is set intelligently based on command-line opts
    if DAEMON == 'Schedd':
        NAME = htcondor.param['FULL_HOSTNAME']
    elif DAEMON == 'Startd':
        NAME = 'slot1@' + htcondor.param['FULL_HOSTNAME']
###

### regular expressions
RE_RUNTIME = re.compile('^(.*)(Runtime.*)')
RE_RUNTIME_IGNORE = re.compile('^DC(Socket|Pipe)')
RE_MONITOR = re.compile('^MonitorSelf(.*)')
###

### constants
RUNTIME_TIME = 'StatsLastUpdateTime' # runtime stats time attribute
RUNTIME_ATTRS = [ # runtime attributes that don't include Runtime
    RUNTIME_TIME
]

MONITOR_ATTRS = [ # monitoring attributes that don't start with SelfMonitor
    'DCCommandsPerSecond_4m',
    'DCCommandsPerSecond_20m',
    'DCCommandsPerSecond_4h',
    'DCSelectWaittime',
    'DCPumpCycleCount',
    'DCPumpCycleSum',
    'Name'
]

PROBES = { # map regex group output to probe types
    '': 'Count',
    'Runtime': 'Runtime',
    'RuntimeMin': 'Min',
    'RuntimeMax': 'Max',
    'RuntimeAvg': 'Avg',
    'RuntimeStd': 'Std',
}

# runtime table columns
TABLE_COLS = ['Runtime', 'InstAvg', 'Avg', 'CountRate', 'Count', 'Max', 'Item']

# daemons that use direct query
DIRECT_DAEMONS = ['Schedd', 'Startd']
###

### small helper functions ###
def hSize(size):
    """Returns a tuple of human-readable byte size (up to petabytes)"""
    suffixes = ['B', 'K', 'M', 'G', 'T', 'P']
    for i in xrange(len(suffixes)):
        if not (size/10**(i*3) > 0):
            break
    i = max(i-1, 0)
    return (float(size)/10**(i*3), suffixes[i])

def hSizeStr(size, decimals=1):
    """Returns a human-readable string representation of byte size"""
    fmt = '{0:.%df}{1}' % (decimals,) # smash number and suffix together (99.9G)
    return fmt.format(*hSize(size))

def hTime(seconds):
    """Returns a human-readable time interval (datetime.timedelta)"""
    return datetime.timedelta(seconds=seconds)

def nLen(n):
    """Returns string length of int(n)"""
    if not n:
        return 0
    return len(str(int(n)))
###

### ClassAd functions ###
def updateClassAd(collector, daemon, name, statistics = 'All:2', direct = False):
    """Returns an updated ClassAd from a HTCondor daemon"""

    # Begin building a list of keyword arguments.
    kwargs = {'statistics': statistics}
        
    if direct:
        # If we're doing a direct query, daemon_type and name must be defined.
        kwargs['daemon_type'] = htcondor.DaemonTypes.names[daemon]
        kwargs['name'] = name

        # Do a direct query on the given daemon at the given hostname.
        # Return the ClassAd and the number of ClassAds queried (always 1).
        return (collector.directQuery(**kwargs), 1)
    
    else:
        # If we're doing a regular query, ad_type must be defined
        # and the ClassAd should be constrained to the given hostname.
        kwargs['ad_type'] = htcondor.AdTypes.names[daemon]
        if name:
            constraint = 'Name =?= {0}'.format(classad.quote(name))
            kwargs['constraint'] = constraint
        else:
            name = '(unspecified)'

        # Do a regular query on the given daemon at the given hostname.
        ads = collector.query(**kwargs)

        # A regular query can give multiple ClassAds, e.g. if there are multiple
        # daemons running on the same host (rare, usually duplicate daemon).

        if len(ads) == 0: # if no ClassAds, then exit
            sys.stderr.write((
                'Error: Received {0} ClassAds from the {1} named {2} '
                'from the Collector at {3}.\n'
                ).format(len(ads), daemon, name, POOL))
            sys.exit(1)

        # Return the first ClassAd and the number of ClassAds queried.
        return (ads[0], len(ads))

def parseClassAd(ad, attrs=[]):
    """Returns dicts with Runtime and SelfMonitor stats from a ClassAd

    A list of additional attributes can be passed via the attrs parameter.
    These additional attributes are added to the runtime dict."""

    # Initialize dicts.
    runtime_stats = {}
    monitor_stats = {}

    # Loop over the attributes in the ClassAd
    for attr in ad:

        # save attributes that match the constant lists of attributes
        if attr in MONITOR_ATTRS:
            monitor_stats[attr] = ad.get(attr)
        elif attr in RUNTIME_ATTRS:
            runtime_stats[(attr, None)] = ad.get(attr)

        # save attributes that the user defined on the command-line
        elif attr in attrs:
            runtime_stats[(attr, 'Count')] = ad.get(attr)

        # save attributes that match the regular expressions
        elif RE_MONITOR.match(attr):
            monitor_stats[RE_MONITOR.match(attr).group(1)] = ad.get(attr)
        elif RE_RUNTIME_IGNORE.match(attr):
            continue # skip ignored attrs
        elif RE_RUNTIME.match(attr):
            attr_basename, probe_type = RE_RUNTIME.match(attr).groups()
            
            if not probe_type in PROBES:
                continue # skip runtime attrs that don't fit our list of probes
            probe_name = PROBES[probe_type]
            
            runtime_stats[(attr_basename, probe_name)] = ad.get(attr)

            # also must grab the counting attrs (don't end in Runtime)
            if not (attr_basename, 'Count') in runtime_stats:
                runtime_stats[(attr_basename, 'Count')] = ad.get(attr_basename)

    return (runtime_stats, monitor_stats)
###

### computation functions ###
def computeDutyCycle(old_ad, new_ad):
    """Calculates and returns the duty cycle"""

    if not old_ad:
        return None
    if not ('DCSelectWaittime' in new_ad):
        return None
    
    # compute differences between ads
    dSelectWaitTime = new_ad['DCSelectWaittime'] - old_ad['DCSelectWaittime']
    dPumpCycleCount = new_ad['DCPumpCycleCount'] - old_ad['DCPumpCycleCount']
    dPumpCycleSum = new_ad['DCPumpCycleSum'] - old_ad['DCPumpCycleSum']

    # compute duty cycle
    duty_cycle = 0
    if (dPumpCycleCount > 0) and (dPumpCycleSum > 1e-9):
        duty_cycle = 1 - dSelectWaitTime/float(dPumpCycleSum)

    return duty_cycle

def commandsPerSecond(ad):
    """Returns the Commands Per Second stat with the longest averaging period"""

    # attrs ordered by longest to shortest averaging period
    attrs = ['DCCommandsPerSecond_4h',
                 'DCCommandsPerSecond_20m',
                 'DCCommandsPerSecond_4m']

    # get the commands per second in same order as attrs
    ops = [ad[attr] for attr in attrs if attr in ad]

    # return the value for the longest average period (if it exists)
    if ops:
        return ops[0]
    else:
        return None

def computeCountRate(attr, old_ad, new_ad):
    """Returns the count rate (per minute) of attr between queries"""

    # try to get an accurate time difference
    if (RUNTIME_TIME, None) in old_ad:
        old_time = old_ad[(RUNTIME_TIME, None)]
        new_time = new_ad[(RUNTIME_TIME, None)]
    
        if (not old_time) or (not new_time):
            return None
        dt = (new_time - old_time)/60.

    else: # otherwise just use the delay (Startd does not return RUNTIME_TIME)
        dt = DELAY/60.

    # count difference
    old_count = old_ad[(attr, 'Count')]
    new_count = new_ad[(attr, 'Count')]

    if (not old_count) or (not new_count):
        return None
    dx = new_count - old_count

    if (dt <= 0): # probably shouldn't get here, but just in case
        return None # to prevent divide by zero

    # return the rate
    return dx/dt

def computeInstAvg(attr, old_ad, new_ad):
    """Returns the runtime/count average of attr between queries"""

    # runtime difference
    old_runtime = old_ad[(attr, 'Runtime')]
    new_runtime = new_ad[(attr, 'Runtime')]

    if (not old_runtime) or (not new_runtime):
        return None
    dx = new_runtime - old_runtime
    
    # count difference
    old_count = old_ad[(attr, 'Count')]
    new_count = new_ad[(attr, 'Count')]

    if (not old_count) or (not new_count):
        return None
    dy = new_count - old_count
    
    if dy == 0: # prevent divide by zero
        return None

    # return the average
    return dx/float(dy)
###

### table building functions ###
def buildSelfMonitorTable(stats):
    """Builds the SelfMonitor stats table"""

    table = {}

    table['Name'] = stats['Name']
    table['Time'] = str(datetime.datetime.fromtimestamp(stats['Time']))
    table['Memory'] = hSizeStr(stats['ImageSize'])
    table['RSS'] = hSizeStr(stats['ResidentSetSize'])

    # older versions of HTCondor may not have these
    if 'SysCpuTime' in stats:
        table['SysCpuTime'] = hTime(stats['SysCpuTime'])
    if 'UserCpuTime' in stats:
        table['UserCpuTime'] = hTime(stats['UserCpuTime'])

    if not ('DCSelectWaittime' in stats):
        pass
    elif stats['DutyCycle']:
        table['DutyCycle'] = '{0:.2%}'.format(stats['DutyCycle'])
    else:
        table['DutyCycle'] = 'defined on update'

    if not ('DCCommandsPerSecond_4m' in stats):
        pass
    elif stats['OpsPerSec']:
        table['OpsPerSec'] = '{0:.3f}'.format(stats['OpsPerSec'])
    else:
        table['OpsPerSec'] = 'undefined'
        
    return table

def buildRuntimeTable(old_ad, new_ad):
    """Builds the Runtime stats table"""

    table = {}

    # check that the ad is new
    if (RUNTIME_TIME, None) in old_ad:
        old_time = old_ad[(RUNTIME_TIME, None)]
        new_time = new_ad[(RUNTIME_TIME, None)]
        if old_time >= new_time:
            return False

        # If RUNTIME_TIME is not in the ClassAd, which is the case when querying
        # a startd, then must assume that the ad is new.

    # loop over the various runtime attributes
    for (attr, probe) in new_ad:

        # skip probes that won't get printed
        if not (probe in TABLE_COLS):
            continue

        # initialize the row in the table and remove ^DC from the attribute name
        if not (attr in table):
            col = 'Item'
            table[attr] = [None]*len(TABLE_COLS)
            table[attr][TABLE_COLS.index(col)] = re.sub(r'DC', '', attr)

        # store the data in the table
        col = probe
        table[attr][TABLE_COLS.index(col)] = new_ad[(attr, probe)]

        # compute extra column values if we're on the appropriate probe
        if probe == 'Count':
            col = 'CountRate'
            table[attr][TABLE_COLS.index(col)] = computeCountRate(attr,
                 old_ad, new_ad)
            
        if probe == 'Runtime':
            col = 'InstAvg'
            table[attr][TABLE_COLS.index(col)] = computeInstAvg(attr, old_ad, new_ad)

    # return a list of tuples (sortable) where Count is defined
    table = [x for x in table.values() if x[TABLE_COLS.index('Count')]]
    return table

### screen printing functions ###
def getConsoleSize():
    """Returns console size"""
    
    try: # should work on Linux
        max_rows, max_cols = [int(x) for x in
                                  os.popen('stty size', 'r').read().split()]
    except Exception: # otherwise assume standard console size
        max_rows = 25
        max_cols = 80

    return (max_rows, max_cols)

def clearScreen():
    """Clears the current console output"""
    sys.stdout.write('\n\n') # add a few blank lines
    sys.stdout.flush()
    if os.name == 'nt':
        os.system('cls')
    else:
        os.system('clear')

def printSelfMonitorTable(stats, daemon, n_ads):
    """Prints the SelfMonitor stats table"""
    
    # get the table 
    table = buildSelfMonitorTable(stats)

    # build the output
    tab = 4*' '
    lines = []
    newline = '\n'
    nrows = 0

    # warn if multiple ads and give a hint about what to do
    if n_ads > 1:
        lines.append(('Warning: '
       'Received {0} {1} ClassAds from the Collector.\n'
       'Displaying the ClassAd for "{2}".\n'
       'Consider setting -n DAEMON_NAME.\n'
        '\n').format(n_ads, daemon, table['Name']))
        nrows += 4

    lines.append('{0} status at {1}:'.format(daemon, table['Time']))
    lines.append(newline)
    nrows += 1

    if 'SysCpuTime' in table:
        lines.append('System CPU Time: {0:<15s}'.format(table['SysCpuTime']))
    if ('SysCpuTime' in table) and ('UserCpuTime' in table):
        lines.append(tab)
    if 'UserCpuTime' in table:
        lines.append('User CPU Time: {0:<s}'.format(table['UserCpuTime']))
    if ('UserCpuTime' in table) or ('UserCpuTime' in table):
        lines.append(newline)
        nrows += 1
        
    lines.append('Memory: {0:<24s}'.format(table['Memory']))
    lines.append(tab)
    lines.append('RSS: {0:<s}'.format(table['RSS']))
    lines.append(newline)
    nrows += 1

    if 'DutyCycle' in table:
        lines.append('Duty Cycle: {0:<20s}'.format(table['DutyCycle']))
    if ('DutyCycle' in table) and ('OpsPerSec' in table):
        lines.append(tab)
    if 'OpsPerSec' in table:
        lines.append('Ops/second: {0:<s}'.format(table['OpsPerSec']))
    if ('DutyCycle' in table) or ('OpsPerSec' in table):
        lines.append(newline)
        nrows += 1

    lines.append(newline)
    nrows += 1
    
    # clear the screen
    clearScreen()

    # write the table
    sys.stdout.write(''.join(lines))
    sys.stdout.flush()

    # return the number of rows used
    return nrows

def printTimer(delay):
    """Prints a countdown to the next update"""
    now = datetime.datetime.utcnow()
    refresh_time = now + datetime.timedelta(seconds=delay)
    time_left = refresh_time - now
    while time_left > datetime.timedelta(seconds=0):
        # rewind the line (\r) and print countdown each second
        sys.stdout.write('\rUpdating ClassAd in {0:>5d} s'.format(
            time_left.seconds))
        sys.stdout.flush()
        sleep(1)
        time_left = refresh_time - datetime.datetime.utcnow()
    sys.stdout.write('\rUpdating ClassAd ' + 10*'.')
    sys.stdout.flush()

def printRuntimeTable(old_ad, new_ad, sort='Runtime',
                          monitor_rows=5, max_rows=25, max_cols=80):
    """Prints the Runtime stats table"""
    
    # get the table
    table = buildRuntimeTable(old_ad, new_ad)

    # sort the table
    table.sort(key = itemgetter(TABLE_COLS.index(sort)), reverse = True)

    # truncate the table
    nrows = max_rows - (monitor_rows + 3)
    table = table[:nrows]

    # more user-friendly column headers
    col_display_names = ['Runtime', 'Inst.Avg', 'Avg', 'Count/Min',
                             'Count', 'Max', 'Item']

    # decimal points used per column
    col_fp = [1, 3, 3, 1, 0, 3]

    # determine the length of each numeric column dynamically
    col_header_fmt = []
    col_fmt = []
    line_len = 0

    for i, col in enumerate(col_display_names[:-1]):
        n_len = nLen(max(table, key=itemgetter(i))[i]) + col_fp[i] + 1
        col_len = max(len(col), n_len)
        line_len += col_len + 1

        col_header_fmt.append('{%d:>%d.%ds}' % (i, col_len, col_len))
        col_fmt.append('{0:>%d.%df}' % (col_len, col_fp[i]))

    # the attribute name column is whatever is leftover from max_cols
    col_header_fmt.append('{%d:<.%ds}\n' % (i + 1, max_cols - line_len))
    col_fmt.append('{0:<%ds}' % (max_cols - line_len))

    # build the row format statement
    row_fmt = ' '.join(col_header_fmt)

    # print the header
    sys.stdout.write(row_fmt.format(*col_display_names))

    # print the rows
    for row in table:
        cols = []
        
        # format each column and store it in cols
        for col, fmt in zip(row, col_fmt):
            if col == None:
                cols.append('n/a')
            else:
                cols.append(fmt.format(col))

        # print the columns
        sys.stdout.write(row_fmt.format(*cols))

    sys.stdout.flush()
###
    
### run the script ###
if __name__ == '__main__':

    collector = htcondor.Collector(opts.pool)
    direct = DAEMON in DIRECT_DAEMONS
    (old_monitor, old_runtime) = (None, None)
    
    # Wrap execution to catch ctrl+c
    try:

        # Loop until ctrl+c
        while True:

            # Get new ads
            ad, n_ads = updateClassAd(collector, DAEMON, NAME, direct = direct)
            runtime, monitor = parseClassAd(ad, ATTRIBUTES)
            
            # Compute duty cycle
            monitor['DutyCycle'] = computeDutyCycle(old_monitor, monitor)

            # Get the ops per second
            monitor['OpsPerSec'] = commandsPerSecond(monitor)
        
            # Print health table
            monitor_rows = printSelfMonitorTable(monitor, DAEMON, n_ads)

            # Print runtime table
            if old_runtime:
                max_rows, max_cols = getConsoleSize()
                printRuntimeTable(old_runtime, runtime, SORT,
                                      monitor_rows, max_rows, max_cols)

            # Save old ads
            old_runtime, old_monitor = (deepcopy(runtime), deepcopy(monitor))

            # Print countdown to next update
            printTimer(DELAY)

    except KeyboardInterrupt:
        sys.stdout.write('\n')
        sys.stdout.flush()

###
