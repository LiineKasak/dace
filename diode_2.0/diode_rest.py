#!flask/bin/python

import dace
import dace.frontend.octave.parse as octave_frontend
import dace.frontend.python.parser as python_frontend
from diode.optgraph.DaceState import DaceState
from dace.transformation.optimizer import SDFGOptimizer
from flask import Flask, Response, request, abort, make_response, jsonify, send_from_directory
import json
import re
from diode.remote_execution import Executor, AsyncExecutor

import traceback, os, threading, queue, time

# Enum imports
from dace.types import AccessType
from dace import ScheduleType, Language, StorageType

app = Flask(__name__)

enum_list = ['AccessType', 'ScheduleType', 'Language', 'StorageType']

es_ref = []

config_lock = threading.Lock()

RUNNING_TIMEOUT = 3


class ConfigCopy:
    """
        Copied Config for passing by-value
    """

    def __init__(self, config_values):
        self._config = config_values

    def get(self, *key_hierarchy):

        current_conf = self._config
        for key in key_hierarchy:
            current_conf = current_conf[key]

        return current_conf

    def get_bool(self, *key_hierarchy):
        from dace.config import _env2bool
        res = self.get(*key_hierarchy)
        if isinstance(res, bool):
            return res
        return _env2bool(str(res))

    def set(self, *key_hierarchy, value=None, autosave=False):
        raise Exception("ConfigCopy does not allow setting values!")


class ExecutorServer:
    """
       Implements a server scheduling execution of dace programs 
    """

    def __init__(self):

        self._command_queue = queue.Queue(
        )  # Fast command queue. Must be polled often (< 30 ms response time)
        self._executor_queue = queue.Queue(
        )  # Run command queue. Latency not critical

        _self = self

        def helper():
            _self.loop()

        def ehelper():
            _self.executorLoop()

        self._task_dict = {}
        self._run_num = 0

        self._running = True
        self._thread = threading.Thread(target=helper, daemon=True)
        self._thread.start()

        self._executor_thread = threading.Thread(target=ehelper, daemon=True)
        self._executor_thread.start()

        self._current_runs = {}
        self._orphaned_runs = {}

        self._oplock = threading.Lock()

        self._run_cv = threading.Condition(
        )  # Used to trickle run tasks through (as the tasks are run in a thread)
        self._slot_available = True  # True if the target machine has a slot for running a program

        self._perfdata_available = {}  # Dict mapping client_id => .can-path
        # (NOTE: We do not handle the raw data in DIODE2.0, i.e. no perfdata.db)
        # (this may change later)

        self._ticket_counter = 0
        self._command_results = {}  # Dict mapping ticket => command result

    def executorLoop(self):
        while self._running:
            self.consume_programs()

    def loop(self):
        while self._running:
            self.consume()

    def waitForCommand(self, ticket):
        while True:
            try:
                with self._oplock:
                    ret = self._command_results[ticket]
                    del self._command_results[ticket]
            except:
                time.sleep(2)
                continue
            return ret

    def addCommand(self, cmd):
        import random
        with self._oplock:
            cmd['ticket'] = self._ticket_counter
            self._ticket_counter += 1
            self._command_queue.put(cmd)
            print("Added command to queue")
            return cmd['ticket']

    def consume_programs(self):

        try:
            cmd = self._executor_queue.get(timeout=3)

            #print("cmd: " + str(cmd))

            if cmd['cmd'] == "run":
                while True:
                    with self._run_cv:
                        if self._slot_available:
                            break
                    import time
                    time.sleep(0.5)

                with self._run_cv:
                    self._slot_available = False
                    print("Running task")

                    self._task_dict[cmd['index']]['state'] = 'running'

                    runner = self.run(
                        cmd['cot'], {
                            'index': cmd['index'],
                            'config_path': cmd['config_path'],
                            'client_id': cmd['cid'],
                            'reset-perfdata': cmd['reset-perfdata'],
                            'perfopts': cmd['opt']['perfopts']
                        })
                    print("Wait for oplock")
                    with self._oplock:
                        self._current_runs[cmd['cid']] = runner

                    import time

                    # Wait a predefined time for clients to catch up on the outputs
                    time.sleep(RUNNING_TIMEOUT)
                    with self._oplock:
                        run_locally = True
                        try:
                            x = self._current_runs[cmd['cid']]
                        except:
                            run_locally = False

                    if run_locally:
                        print("running locally")

                        def tmp():
                            with self._oplock:
                                del self._current_runs[cmd['cid']]
                                try:
                                    c = self._orphaned_runs[cmd['cid']]
                                except:
                                    self._orphaned_runs[cmd['cid']] = []
                                self._orphaned_runs[cmd['cid']].append([])
                            print("Starting runner")
                            for x in runner():
                                self._orphaned_runs[cmd['cid']][-1] += x

                        # Because this holds locks (and the output should be generated even if nobody asks for it immediately), this is run when the timeout for direct interception
                        tmp()
            elif cmd['cmd'] == 'control':
                # Control operations that must be synchronous with execution (e.g. for cleanup, storage operations)
                with self._oplock:
                    self._task_dict[cmd['index']]['state'] = 'running'

                if cmd['operation'] == 'startgroup':
                    from diode.db_scripts.db_setup import db_setup
                    perf_tmp_dir = self.getPerfdataDir(cmd['cid'])
                    perfdata_path = os.path.join(perf_tmp_dir, "perfdata.db")

                    # Clean database and create tables
                    db_setup(perf_tmp_dir)

                elif cmd['operation'] == 'remove_group':
                    perfdir = self.getPerfdataDir(cmd['cid'])
                    perfdata_path = os.path.join(perfdir, "perfdata.db")
                    os.remove(perfdata_path)
                    os.rmdir(perf_tmp_dir)

                elif cmd['operation'] == 'endgroup':
                    print("Ending group")
                    from diode.db_scripts.sql_to_json import MergeRuns, Conserver
                    from dace.config import Config

                    config_path = cmd['config_path']

                    with config_lock:
                        Config.load(config_path)
                        repetitions = Config.get("execution", "general",
                                                 "repetitions")

                    perf_tmp_dir = self.getPerfdataDir(cmd['cid'])
                    perfdata_path = os.path.join(perf_tmp_dir, "perfdata.db")
                    can_path = os.path.join(perf_tmp_dir, 'current.can')

                    mr = MergeRuns()
                    mr.mergev2(perfdata_path)
                    print("Merged into " + perfdata_path)

                    cons = Conserver()
                    # TODO: Add sdfgs
                    cons.conserveAll(
                        perfdata_path,
                        can_path,
                        "",
                        repetitions,
                        clear_existing=False)

                    print("Merged and Conserved!")
                    self._perfdata_available[cmd['cid']] = can_path

                with self._oplock:
                    del self._task_dict[cmd['index']]

        except queue.Empty:
            return

    def consume(self):

        try:
            cmd = self._command_queue.get(timeout=3)

            if isinstance(cmd, str):
                pass
            else:
                command = cmd['cmd']
                print("Got command " + command)
                if command == "get_perfdata":
                    import sqlite3

                    try:
                        conn = sqlite3.connect(
                            self._perfdata_available[cmd['cid']])
                    except:
                        self._command_results[cmd[
                            'ticket']] = "Error: Perfdata not available"
                        print("Errored-out!")
                        return

                    querystring = "SELECT * FROM AnalysisResults "

                    for_node = cmd['for_node']
                    for_program = cmd['for_program']
                    for_supersection = cmd['for_supersection']
                    for_section = cmd['for_section']

                    query_list = [('forUnifiedID', for_node), ('forProgram',
                                                               for_program),
                                  ('forSuperSection', for_supersection),
                                  ('forSection', for_section)]

                    query_values = []
                    first = True
                    for x in query_list:
                        name, val = x
                        if val == None:
                            continue
                        if first:
                            querystring += " WHERE "
                        else:
                            querystring += " AND "
                        first = False
                        querystring += name + " = ?"
                        query_values.append(val)

                    querystring += ";"

                    print("querystring: " + str(querystring))
                    print("tuple: " + str(tuple(query_values)))

                    c = conn.cursor()
                    c.execute(querystring, tuple(query_values))

                    result = c.fetchall()

                    print("setting result for ticket " + str(cmd['ticket']))
                    self._command_results[cmd['ticket']] = json.dumps(result)

                    conn.close()

                    print("Success reading database")

        except queue.Empty:
            return

    def getExecutionOutput(self, client_id):
        import time
        ret = None
        err_count = 0
        while ret == None:
            with self._oplock:
                try:
                    ret = self._current_runs[client_id]
                    del self._current_runs[client_id]
                except:
                    err_count += 1
                    if err_count < 10:  # Give 10 seconds of space for compilation and distribution
                        time.sleep(1)
                        continue

                    def egen():
                        yield "{'error': 'Failed to get run reference'}"

                    return egen
                return ret

    def stop(self):
        self._running = False

    def lock(self):
        self._oplock.acquire()

    def unlock(self):
        self._oplock.release()

    def getPerfdataDir(self, client_id):
        import tempfile

        if not os.path.isdir("perfdata-dir/"):
            os.mkdir("perfdata-dir")

        tpath = "perfdata-dir/" + client_id

        try:
            os.mkdir(tpath)
        except:
            pass
        perf_tmp_dir = tpath
        return perf_tmp_dir

    def addRun(self, client_id, compilation_output_tuple, more_options):

        config_path = "./client_configs/" + client_id + ".conf"
        if not os.path.isdir("./client_configs/"):
            os.mkdir("./client_configs/")
        if not os.path.isfile(config_path):
            # Config not (yet) available, load default and copy
            with config_lock:
                from dace.config import Config
                Config.load()
                Config.save(config_path)

        if isinstance(compilation_output_tuple, str):
            # Group command
            gc = compilation_output_tuple
            val = {
                'cid': client_id,
                'cmd': 'control',
                'index': self._run_num,
                'operation': None,
                'config_path': config_path,
                'state': "pending"
            }
            if gc == "start":
                val['operation'] = 'startgroup'
            elif gc == "end":
                val['operation'] = 'endgroup'
            else:

                def g():
                    yield '{ "error": "Unknown group operation" }'

                return g

            with self._oplock:
                self._executor_queue.put(val)
                self._task_dict[self._run_num] = val
                self._run_num += 1
            return

        with self._oplock:
            val = {
                'index': self._run_num,
                'type': 'run',
                'cid': client_id,
                'config_path': config_path,
                'cmd': 'run',
                'cot': compilation_output_tuple,
                'opt': more_options,
                'state': 'pending',
                'reset-perfdata': False
            }
            self._executor_queue.put(item=val)

            self._task_dict[self._run_num] = val
            self._run_num += 1

        def error_gen():
            yield '{ "error": "Run was scheduled. Please poll until ready or longpoll." }'

        return error_gen

    def run(self, cot, options):

        print("=> Run called")

        print("Options: " + str(options))
        compilation_output_tuple = cot
        runindex = options['index']
        config_path = options['config_path']
        client_id = options['client_id']
        perfopts = options['perfopts']
        sdfgs, code_tuples, sdfg_props, dace_state = compilation_output_tuple

        terminal_queue = queue.Queue()

        # Generator used to pass the serial output through (using HTTP1.1. streaming)
        def output_feeder(output):
            if isinstance(output, str):
                # It's already in a usable format
                pass
            else:
                try:
                    output = output.decode('utf-8')
                except UnicodeDecodeError:
                    # Try again escaping
                    output = output.decode('unicode_escape')
            terminal_queue.put(output)

        def runner():
            print("Trying to get lock")
            with self._run_cv:
                print("Run starting")

                perfmode = perfopts['mode']
                perfcores = perfopts['core_counts']

                with config_lock:
                    from dace.config import Config
                    Config.load(config_path)
                    if perfmode == "noperf":
                        Config.set(
                            "instrumentation", "enable_papi", value=False)
                    else:
                        Config.set(
                            "instrumentation", "enable_papi", value=True)
                        Config.set(
                            "instrumentation", "papi_mode", value=perfmode)
                        Config.set(
                            "instrumentation",
                            "sql_database_file",
                            value=self.getPerfdataDir(client_id) +
                            "/perfdata.db")
                        Config.set(
                            "instrumentation",
                            "thread_nums",
                            value=str(perfcores))

                    # Copy the config - this allows releasing the config lock without suffering from potential side effects
                    copied_config = ConfigCopy(Config._config)

                self._slot_available = False
                dace_state.set_is_compiled(False)

                async_executor = AsyncExecutor(None, True, None, None)
                async_executor.autoquit = True
                async_executor.executor.output_generator = output_feeder
                async_executor.executor.setConfig(copied_config)
                async_executor.run_async(dace_state)
                async_executor.to_thread_message_queue.put("forcequit")

                while async_executor.running_thread.is_alive():
                    try:
                        new = terminal_queue.get(block=True, timeout=1)
                        yield new
                    except:
                        # Check if the thread is still running
                        continue

                with self._oplock:
                    # Delete from the tasklist
                    del self._task_dict[runindex]

                    print("Run done, notifying")
                    self._slot_available = True

        return runner


@app.route('/client/<path:path>', methods=['GET'])
def index(path):
    """
        This is an http server (on the same port as the REST API).
        It serves the files from the 'client'-directory to user agents.
        Note: This is NOT intended for production environments and security is disregarded!
    """
    return send_from_directory("client", path)


@app.route('/dace/api/v1.0/getEnum/<string:name>', methods=['GET'])
def getEnum(name):
    """   
        Helper function to enumerate available values for `ScheduleType`.

        Returns:
            enum: List of string-representations of the values in the enum
    """

    valid_params = enum_list

    if name not in valid_params:
        # To protect against arbitrary code execution, this request is refused
        print("Enum type '" + str(name) + "' is not in Whitelist")
        abort(400)

    return jsonify({'enum': [str(e).split(".")[-1] for e in eval(name)]})


def collect_all_SDFG_nodes(sdfg):
    ret = []
    for sid, state in enumerate(sdfg.nodes()):
        for nid, node in enumerate(state.nodes()):
            ret.append(('s' + str(sid) + '_' + str(nid), node))
    return ret


def split_nodeid_in_state_and_nodeid(nodeid):
    match = re.match(r"s(\d+)_(\d+)", nodeid)
    if match:
        ids = match.groups()
        return int(ids[0]), int(ids[1])
    else:
        match = re.match(r"dummy_(\d+)", nodeid)
        if match:
            ids = match.groups()
            return int(ids[0]), None
        else:
            raise ValueError("Node ID " + nodeid + " has the wrong form")
            return None


def properties_to_json_list(props):
    ret = []
    for x, val in props:
        try:
            typestr = x.dtype.__name__
        except:
            # Try again, it might be an enum
            try:
                typestr = x.enum.__name__
            except:
                typestr = 'None'

        # Special case of CodeProperty
        if isinstance(x, dace.properties.CodeProperty):
            typestr = "CodeProperty"

            if val == None:
                continue

            val = x.to_string(val)

        # Special case of DebugInfoProperty: Transcribe to object (this is read-only)
        if isinstance(x, dace.properties.DebugInfoProperty):
            typestr = "DebugInfo"

            if val == None:
                continue

            nval = {
                "filename": val.filename,
                "start_line": val.start_line,
                "end_line": val.end_line,
                "start_col": val.start_column,
                "end_col": val.end_column
            }

            val = json.dumps(nval)

        ret.append({
            "name": str(x.attr_name),
            "desc": str(x.desc),
            "type": typestr,
            "default": str(x.default),
            "value": str(val)
        })
    return ret


def get_SDFG_node_properties(sdfg, nodeid_or_obj):
    if isinstance(nodeid_or_obj, str):
        sid, nid = split_nodeid_in_state_and_nodeid(nodeid_or_obj)
        node = sdfg.nodes()[sid].nodes()[nid]
    else:
        node = nodeid_or_obj

    props = node.properties()
    return properties_to_json_list(props)


def get_all_SDFG_node_properties(sdfg):
    nodelist = collect_all_SDFG_nodes(sdfg)
    sdfg_props = []

    for x in nodelist:
        _id, _node = x
        sid, nid = split_nodeid_in_state_and_nodeid(_id)
        props = get_SDFG_node_properties(sdfg, _node)
        sdfg_props.append({
            'state_id': str(sid),
            'node_id': str(nid),
            'params': props
        })

    return sdfg_props


def set_properties_from_json(obj, prop, sdfg=None):
    if prop['default'] == "None" and sdfg == None:
        # This dropout is only valid for transformations
        # Properties without a default are transformation-generic and should not be settable.
        pass
    else:
        # Catching some transcription errors
        val = prop['value'] == 'True' if prop['type'] == 'bool' else prop[
            'value']
        if any(map(lambda x: x in prop['type'], enum_list)):
            # This is an enum. If the value was fully qualified, it needs to be trimmed
            if '.' in val:
                val = val.split('.')[-1]
        dace.properties.set_property_from_string(
            prop['name'], obj, json.dumps(val), sdfg, from_json=True)


def applySDFGProperty(sdfg, property_element, step=None):

    try:
        prop_step = int(property_element['step'])
    except:
        print("[Warning] Prop step was not provided")
        prop_step = 0
        print("applySDFGProperty: step " + str(step) + ", prop_step: " +
              str(prop_step))
    if step != None and prop_step != step:
        # Step mismatch; ignore
        return sdfg

    sid = int(property_element['state_id'])
    nid = int(property_element['node_id'])
    node = sdfg.find_node(sid, nid)

    for prop in property_element['params']:
        set_properties_from_json(node, prop, sdfg)

    return sdfg


def applySDFGProperties(sdfg, properties, step=None):

    for x in properties:
        applySDFGProperty(sdfg, x, step)

    return sdfg


def applyOptPath(sdfg, optpath, useGlobalSuffix=True, sdfg_props=[]):
    # Iterate over the path, applying the transformations
    print("optpath" + str(optpath))
    global_counter = {}
    if sdfg_props == None: sdfg_props = []
    step = 0
    for x in optpath:
        optimizer = SDFGOptimizer(sdfg, inplace=True)
        matching = optimizer.get_pattern_matches()

        # Apply properties (will automatically apply by step-matching)
        sdfg = applySDFGProperties(sdfg, sdfg_props, step)

        for pattern in matching:
            name = type(pattern).__name__

            if useGlobalSuffix:
                if name in global_counter:
                    global_counter[name] += 1
                else:
                    global_counter[name] = 0
                tmp = global_counter[name]

                if tmp > 0:
                    name += "$" + str(tmp)

            if name == x['name']:
                #for prop in x['params']['props']:
                #if prop['name'] == 'subgraph': continue
                #set_properties_from_json(pattern, prop, sdfg)

                dace.properties.Property.set_properties_from_json(
                    pattern, x['params']['props'], context={'sdfg': sdfg})
                pattern.apply_pattern(sdfg)

                if not useGlobalSuffix:
                    break

        step += 1
    sdfg = applySDFGProperties(sdfg, sdfg_props, step)
    return sdfg


def compileProgram(request, language, perfopts=None):
    if not request.json or (('code' not in request.json) and
                            ('sdfg' not in request.json)):
        print("[Error] No input code provided, cannot continue")
        abort(400)

    errors = []
    try:
        optpath = request.json['optpath']
    except:
        optpath = None

    try:
        sdfg_props = request.json['sdfg_props']
    except:
        sdfg_props = None

    if perfopts == None:
        try:
            perf_mode = request.json['perf_mode']
        except:
            perf_mode = None
    else:
        print("Perfopts: " + str(perfopts))
        perf_mode = perfopts

    client_id = request.json['client_id']

    sdfg_dict = {}

    with config_lock:  # Lock the config - the config may be modified while holding this lock, but the config MUST be restored.

        from dace.config import Config
        config_path = "./client_configs/" + client_id + ".conf"
        if os.path.isfile(config_path):
            Config.load(config_path)
        else:
            Config.load()

        if perf_mode != None:
            tmp = perf_mode['mode']
            if tmp != 'noperf':
                Config.set(
                    "instrumentation",
                    "enable_papi",
                    value=True,
                    autosave=False)
                Config.set(
                    "instrumentation",
                    "papi_mode",
                    value=perf_mode['mode'],
                    autosave=False)
                print("Set perfmode to " + perf_mode['mode'])
            else:
                Config.set(
                    "instrumentation",
                    "enable_papi",
                    value=False,
                    autosave=False)

        dace_state = None
        in_sdfg = None
        if "sdfg" in request.json:
            in_sdfg = request.json['sdfg']
            if isinstance(in_sdfg, list):
                if len(in_sdfg) > 1:
                    print("More than 1 sdfg provided!")
                    raise Exception("#TODO: Allow multiple sdfg inputs")
                    abort(400)
                in_sdfg = in_sdfg[0]

            if isinstance(in_sdfg, str):
                in_sdfg = json.loads(in_sdfg)

            if isinstance(in_sdfg, dict):
                # Generate callbacks (needed for elements referencing others)
                def loader_callback(name: str):
                    # Check if already available and if yes, return it
                    if name in sdfg_dict:
                        return sdfg_dict[name]

                    # Else: This function has to recreate the given sdfg
                    sdfg_dict[name] = dace.SDFG.fromJSON_object(
                        in_sdfg[name], {
                            'sdfg': None,
                            'callback': loader_callback
                        })
                    return sdfg_dict[name]

                for k, v in in_sdfg.items():
                    # Leave it be if the sdfg was already created
                    # (this might happen with SDFG references)
                    if k in sdfg_dict: continue
                    sdfg_dict[k] = dace.SDFG.fromJSON_object(
                        v, {
                            'sdfg': None,
                            'callback': loader_callback
                        })
            else:
                in_sdfg = dace.SDFG.fromJSON_object(in_sdfg)
                sdfg_dict[in_sdfg.name] = in_sdfg
        else:
            print("Using code to compile")
            code = request.json['code']
            if (isinstance(code, list)):
                if len(code) > 1:
                    print("More than 1 code file provided!")
                    abort(400)
                code = code[0]
            if language == "octave":
                statements = octave_frontend.parse(code, debug=False)
                statements.provide_parents()
                statements.specialize()
                sdfg = statements.generate_code()
                sdfg.set_sourcecode(code, "matlab")
            elif language == "dace":
                try:
                    dace_state = DaceState(code, "fake", headless=True)
                    for x in dace_state.sdfgs:
                        name, sdfg = x
                        sdfg_dict[name] = sdfg

                except SyntaxError as se:
                    # Syntax error
                    errors.append({
                        'type': "SyntaxError",
                        'line': se.lineno,
                        'offset': se.offset,
                        'text': se.text,
                        'msg': se.msg
                    })
                except ValueError as ve:
                    # DACE-Specific error
                    tb = traceback.format_exc()
                    errors.append({
                        'type': "ValueError",
                        'stringified': str(ve),
                        'traceback': tb
                    })
                except Exception as ge:
                    # Generic exception
                    tb = traceback.format_exc()
                    errors.append({
                        'type': ge.__class__.__name__,
                        'stringified': str(ge),
                        'traceback': tb
                    })

        # The DaceState uses the variable names in the dace code. This is not useful enough for us, so we translate
        copied_dict = {}
        for k, v in sdfg_dict.items():
            copied_dict[v.name] = v
        sdfg_dict = copied_dict

        if len(errors) == 0:
            if optpath != None:
                for sdfg_name, op in optpath.items():
                    try:
                        sp = sdfg_props[sdfg_name]
                    except:
                        # In any error case, just ignore the properties
                        sp = None
                    print("Applying opts for " + sdfg_name)
                    print("Dict: " + str(sdfg_dict.keys()))
                    sdfg_dict[sdfg_name] = applyOptPath(
                        sdfg_dict[sdfg_name], op, sdfg_props=sp)

        if in_sdfg == None and len(errors) == 0:
            if sdfg_props != None:
                for sdfg_name, sp in sdfg_props.items():
                    sdfg_dict[sdfg_name] = applySDFGProperties(
                        sdfg_dict[sdfg_name], sp)

        sdfg_prop_dict = {}
        if len(errors) == 0:
            # Get the properties of every node in the SDFG
            for n, s in sdfg_dict.items():
                sdfg_prop_dict[n] = get_all_SDFG_node_properties(s)

        code_tuple_dict = {}
        if len(errors) == 0:
            from dace.codegen import codegen
            for n, s in sdfg_dict.items():
                code_tuple_dict[n] = codegen.generate_code(s)

        if dace_state == None:
            try:
                dace_state = DaceState("", "fake", sdfg=list(sdfg_dict.values())[0], headless=True)
            except Exception as e:
                traceback.print_exc()
                print("Failed to create DaceState")

        # The config won't save back on its own, and we don't want it to - these changes are transient

        if len(errors) > 0:
            return errors

        return (sdfg_dict, code_tuple_dict, sdfg_prop_dict, dace_state)


def get_transformations(sdfgs):
    opt_per_sdfg = {}

    for sdfg_name, sdfg in sdfgs.items():
        opt = SDFGOptimizer(sdfg)
        ptrns = opt.get_pattern_matches()

        optimizations = []
        for p in ptrns:
            label = type(p).__name__

            nodeids = []
            properties = []
            if p is not None:
                sid = p.state_id
                nodes = list(p.subgraph.values())
                for n in nodes:
                    nodeids.append("s" + str(sid) + "_" + str(n))

                #properties = properties_to_json_list(p.properties())
                properties = json.loads(
                    dace.properties.Property.all_properties_to_json(p))
            optimizations.append({
                'opt_name': label,
                'opt_params': properties,
                'affects': nodeids,
                'children': []
            })

        opt_per_sdfg[sdfg_name] = {'matching_opts': optimizations}
    return opt_per_sdfg


@app.route("/dace/api/v1.0/perfdata/get/", methods=['POST'])
def perfdata_query():
    """
        This function returns the matching Analysis results from the latest CAN.
        NOTE: It does _not_ return the full raw result list. This differs from DIODE1 behavior

        POST-Parameters:
            client_id: string. The client id
            analysis_name: string. The name of the queried analysis
            for_program: string or int. The number of the queried program
            for_node: string or int. The id of the node
            for_supersection: string or int. The number of the queried supersection
            for_section: string or int. The number of the queried section.

            The server returns a JSON-dict of all matching values.
            Omitting a `for_`-Parameter is allowed and excludes this parameter from the filter (matches anything).
            The client id is required.

    """

    try:
        client_id = request.json['client_id']
    except:
        print("Client id not specified, cannot continue")
        abort(400)

    try:
        analysis_name = request.json['analysis_name']
    except:
        analysis_name = None
    try:
        for_program = request.json['for_program']
    except:
        for_program = None
    try:
        for_node = request.json['for_node']
    except:
        for_node = None
    try:
        for_supersection = request.json['for_supersection']
    except:
        for_supersection = None
    try:
        for_section = request.json['for_section']
    except:
        for_section = None

    es = es_ref[0]

    ticket = es.addCommand({
        'cmd': 'get_perfdata',
        'cid': client_id,
        'analysis_name': analysis_name,
        'for_program': for_program,
        'for_node': for_node,
        'for_supersection': for_supersection,
        'for_section': for_section
    })

    # This should not take too long, so for now, we wait here for the executor the serve the request instead of letting the client poll another address

    print("Now waiting for ticket")
    ret = es.waitForCommand(ticket)
    print("Got the ticket, we're done!")

    # jsonify creates a response directly, so we load string => obj first
    return jsonify(json.loads(ret))


@app.route("/dace/api/v1.0/dispatcher/<string:op>/", methods=['POST'])
def execution_queue_query(op):
    es = es_ref[0]
    if op == "list":
        # List the currently waiting tasks
        retlist = []
        for key, val in es._task_dict.items():
            d = {}
            if val['cmd'] == 'run':
                d['index'] = key
                d['type'] = 'run'
                d['client_id'] = val['cid']
                d['options'] = val['opt']
                d['state'] = val['state']
            elif val['cmd'] == 'control':
                d['index'] = key
                d['type'] = 'command'
                d['client_id'] = val['cid']
                d['options'] = val['operation']
                d['state'] = val['state']

            retlist.append(d)
        ret = {}
        ret['elements'] = retlist
        return jsonify(ret)
    else:
        print("Error: op " + str(op) + " not implemented")
        abort(400)


@app.route('/dace/api/v1.0/run/status/', methods=['POST'])
def get_run_status():

    if not request.json or not 'client_id' in request.json:
        print("[Error] No client id provided, cannot continue")
        abort(400)

    es = es_ref[0]

    # getExecutionOutput returns a generator to output to a HTTP1.1 stream
    outputgen = es.getExecutionOutput(request.json['client_id'])
    return Response(outputgen(), mimetype='text/text')


@app.route('/dace/api/v1.0/run/', methods=['POST'])
def run():
    """
        This function is equivalent to the old DIODE "Run"-Button.

        POST-Parameters:
            (Same as for compile(), language defaults to 'dace')
            perfmodes: list including every queried mode
            corecounts: list of core counts (one run for every number of cores)
            
    """

    try:
        perfmodes = request.json['perfmodes']
    except:
        perfmodes = ["noperf"]

    try:
        corecounts = request.json['corecounts']
    except:
        corecounts = [0]

    # Obtain the reference
    es = es_ref[0]

    client_id = request.json['client_id']
    es.addRun(client_id, "start", {})

    for pmode in perfmodes:
        perfopts = {'mode': pmode, 'core_counts': corecounts}
        tmp = compileProgram(request, 'dace', perfopts)
        if len(tmp) > 1:
            sdfgs, code_tuples, sdfg_props, dace_state = tmp
        else:
            # ERROR
            print("An error occurred")
            abort(400)

        more_options = {}
        more_options['perfopts'] = perfopts
        runner = es.addRun(client_id,
                           (sdfgs, code_tuples, sdfg_props, dace_state),
                           more_options)

    es.addRun(client_id, "end", {})

    # There is no state information with this, just the output
    # It might be necessary to add a special field that the client has to filter out
    # to provide additional state information
    return Response(runner(), mimetype="text/text")


@app.route('/dace/api/v1.0/match_optimizer_patterns/', methods=['POST'])
def optimize():
    """
        Returns a list of possible optimizations (transformations) and their properties.
        #TODO: By sending the input code, we force the server to recalculate very often.
        # It might be better to send a serialized SDFG and de-serializing it at the server,
        # or using a (stateful) aging server cache to avoid frequent recalculations


        POST-Parameters:
            input_code: list. Contains all necessary input code files
            [opt] optpath:  list of dicts, as { name: <str>, params: <dict> }. Contains the current optimization path/tree.
                            This optpath is applied to the provided code before evaluating possible pattern matches.

            client_id: <string>:    For later identification. May be unique across all runs, 
                                    must be unique across clients

        Returns:
            matching_opts:  list of dicts, as { opt_name: <str>, opt_params: <dict>, affects: <list>, children: <recurse> }.
                            Contains the matching transformations.
                            `affects` is a list of affected node ids, which must be unique in the current program.
    
    """
    tmp = compileProgram(request, 'dace')
    if len(tmp) > 1:
        sdfgs, code_tuples, sdfg_props, dace_state = tmp
    else:
        # Error
        return jsonify({'error': tmp})

    opt_per_sdfg = get_transformations(sdfgs)
    return jsonify(opt_per_sdfg)


@app.route('/dace/api/v1.0/compile/<string:language>', methods=['POST'])
def compile(language):
    """
        POST-Parameters:
            sdfg: ser. sdfg:    Contains the root SDFG, serialized in JSON-string. If set, options `code` and `sdfg_props` are taken from this value.
                                Can be a list of SDFGs.
                                NOTE: If specified, `code`, `sdfg_prop`, and `language` (in URL) are ignored.
            code: string/list.  Contains all necessary input code files
            [opt] optpath:      list of dicts, as { <sdfg_name/str>: { name: <str>, params: <dict> }}. Contains the current optimization path/tree.
                                This optpath is applied to the provided code before compilation

            [opt] sdfg_props:   list of dicts, as { <sdfg_name/str>: { state_id: <str>, node_id: <str>, params: <dict>, step: <opt int>}}. Contains changes to the default SDFG properties.
                                The step element of the dicts is optional. If it is provided, it specifies the number
                                of optpath elements that preceed it. E.g. a step value of 0 means that the property is applied before the first optimization.
                                If it is omitted, the property is applied after all optimization steps, i.e. to the resulting SDFG

            [opt] perf_mode:    string. Providing "null" has the same effect as omission. If specified, enables performance instrumentation with the counter set
                                provided in the DaCe settings. If null (or omitted), no instrumentation is enabled.

            client_id: <string>:    For later identification. May be unique across all runs, 
                                    must be unique across clients

        Returns:
            sdfg: object. Contains a serialization of the resulting SDFGs.
            generated_code: string.     Contains the output code
            sdfg_props: object. Contains a dict of all properties for
                                every existing node of the sdfgs returned
                                in the sdfg field
    """

    tmp = compileProgram(request, language)
    if len(tmp) > 1:
        sdfgs, code_tuples, sdfg_props, dace_state = tmp
    else:
        # Error
        return jsonify({'error': tmp})

    opts = get_transformations(sdfgs)
    compounds = {}
    for n, s in sdfgs.items():
        compounds[n] = {
            "sdfg": json.loads(s.toJSON()),
            #"sdfg_props": sdfg_props[n],
            "matching_opts": opts[n]['matching_opts'],
            "generated_code": [*map(lambda x: x.code, code_tuples[n])]
        }
    return jsonify({"compounds": compounds})


@app.route('/dace/api/v1.0/decompile/<string:obj>/', methods=['POST'])
def decompile(obj):
    """
        De-compiles (pickles) an SDFG in python binary format.

        POST-Parameters:
            binary: base64 string. The object to pickle (the URL encodes the expected type).
    """

    import base64, pickle

    try:
        b64_data = request.json['binary']
        decoded = base64.decodebytes(b64_data.encode())
    except:
        abort(Response("Invalid input", 400))
    if obj == "SDFG":
        loaded_sdfg = ""
        from dace.sdfg import SDFG

        try:
            loaded_sdfg = SDFG.from_bytes(decoded)
        except:
            abort(Response("The provided file is not a valid SDFG", 400))

        # With the SDFG decoded, we must adhere to the output format of compile() for the best interoperability
        sdfg_name = loaded_sdfg.name
        opts = get_transformations({sdfg_name: loaded_sdfg})
        props = get_all_SDFG_node_properties(loaded_sdfg)

        from dace.codegen import codegen
        gen_code = codegen.generate_code(loaded_sdfg)

        return jsonify({
            "compounds": {
                sdfg_name: {
                    'input_code': loaded_sdfg.sourcecode,
                    'sdfg': loaded_sdfg.toJSON(),
                    'sdfg_props': props,
                    'matching_opts': opts[sdfg_name]['matching_opts'],
                    'generated_code': [*map(lambda x: x.code, gen_code)]
                }
            }
        })

    else:
        print("Invalid object type '" + obj + "' specified for decompilation")
        abort(400)


@app.route('/dace/api/v1.0/diode2/themes', methods=['GET'])
def get_available_ace_editor_themes():
    import glob, os.path
    path = "./client/external_lib/ace/"

    files = [f for f in glob.glob(path + "theme-*.js")]

    filenames = map(os.path.basename, files)

    return jsonify([*filenames])


def get_settings(client_id, name="", cv=None, config_path=""):
    from dace.config import Config

    if cv == None:
        clientpath = "./client_configs/" + client_id + ".conf"
        if os.path.isfile(clientpath):
            Config.load(clientpath)
        else:
            Config.load()

    if cv == None:
        cv = Config.get()
    ret = {}
    for i, (cname, cval) in enumerate(sorted(cv.items())):
        cpath = tuple(list(config_path) + [cname])
        meta = Config.get_metadata(*cpath)

        # A dict contains more elements
        if meta['type'] == 'dict':
            ret[cname] = {
                "value": get_settings(client_id, cname, cval, cpath),
                "meta": meta
            }
            continue
        # Other values can be included directly
        ret[cname] = {"value": cval, "meta": meta}

    return ret


def set_settings(settings_array, client_id):
    from dace.config import Config

    if not os.path.isdir("./client_configs"):
        os.mkdir("./client_configs/")
    clientpath = "./client_configs/" + client_id + ".conf"

    if os.path.isfile(clientpath):
        Config.load(clientpath)
    else:
        Config.load()

    for path, val in settings_array.items():
        path = path.split("/")
        Config.set(*path, value=val)

    Config.save(clientpath)


@app.route('/dace/api/v1.0/preferences/<string:operation>', methods=['POST'])
def diode_settings(operation):
    if operation == "get":
        client_id = request.json['client_id']
        return jsonify(get_settings(client_id))
    elif operation == "set":
        print("request.data: " + str(request.data))
        settings = request.json
        client_id = settings['client_id']
        del settings['client_id']
        return jsonify(set_settings(settings, client_id))
    else:
        return jsonify({"error": "Unsupported operation"})


@app.route('/dace/api/v1.0/status', methods=['POST'])
def status():
    # just a kind of ping/pong to see if the server is running
    return "OK"


if __name__ == '__main__':

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-l", "--localhost", action="store_true",
                    help="Bind to localhost only")

    parser.add_argument("-ld", "--localdace", action="store_true",
                    help="Use local comamnds instead of ssh")

    parser.add_argument("-rd", "--restoredace", action="store_true",
                    help="Restore the backup file")

    args = parser.parse_args()

    if args.restoredace:
        from dace.config import Config
        Config.load("./dace.conf.bak")
        Config.save()

    if args.localdace:
        from dace.config import Config
        Config.load()
        Config.save("./dace.conf.bak")
        Config.load()
        Config.set("execution", "general", "execcmd", value='${command}', autosave=True)
        Config.set("execution", "general", "copycmd_r2l", value='cp ${srcfile} ${dstfile}', autosave=True)
        Config.set("execution", "general", "copycmd_l2r", value='cp ${srcfile} ${dstfile}', autosave=True)
        if not os.path.isdir("./client_configs"):
            os.mkdir("./client_configs")
        Config.save("./client_configs/default.conf")

    es = ExecutorServer()
    es_ref.append(es)
    app.run(host='localhost' if args.localhost else "0.0.0.0", debug=True, use_reloader=False)

    es.stop()
else:
    # Start the executor server
    es = ExecutorServer()
    es_ref.append(es)

    import atexit
    def tmp():
        es.stop()
    atexit.register(tmp)