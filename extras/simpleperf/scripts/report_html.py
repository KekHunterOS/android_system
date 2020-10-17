#!/usr/bin/env python
#
# Copyright (C) 2017 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import collections
import datetime
import json
import os
import sys

from simpleperf_report_lib import ReportLib
from utils import log_info, log_exit
from utils import Addr2Nearestline, get_script_dir, Objdump, open_report_in_browser
from utils import SourceFileSearcher

MAX_CALLSTACK_LENGTH = 750

class HtmlWriter(object):

    def __init__(self, output_path):
        self.fh = open(output_path, 'w')
        self.tag_stack = []

    def close(self):
        self.fh.close()

    def open_tag(self, tag, **attrs):
        attr_str = ''
        for key in attrs:
            attr_str += ' %s="%s"' % (key, attrs[key])
        self.fh.write('<%s%s>' % (tag, attr_str))
        self.tag_stack.append(tag)
        return self

    def close_tag(self, tag=None):
        if tag:
            assert tag == self.tag_stack[-1]
        self.fh.write('</%s>\n' % self.tag_stack.pop())

    def add(self, text):
        self.fh.write(text)
        return self

    def add_file(self, file_path):
        file_path = os.path.join(get_script_dir(), file_path)
        with open(file_path, 'r') as f:
            self.add(f.read())
        return self

def modify_text_for_html(text):
    return text.replace('>', '&gt;').replace('<', '&lt;')

class EventScope(object):

    def __init__(self, name):
        self.name = name
        self.processes = {}  # map from pid to ProcessScope
        self.sample_count = 0
        self.event_count = 0

    def get_process(self, pid):
        process = self.processes.get(pid)
        if not process:
            process = self.processes[pid] = ProcessScope(pid)
        return process

    def get_sample_info(self, gen_addr_hit_map):
        result = {}
        result['eventName'] = self.name
        result['eventCount'] = self.event_count
        processes = sorted(self.processes.values(), key=lambda a: a.event_count, reverse=True)
        result['processes'] = [process.get_sample_info(gen_addr_hit_map)
                               for process in processes]
        return result

    @property
    def threads(self):
        for process in self.processes.values():
            for thread in process.threads.values():
                yield thread

    @property
    def libraries(self):
        for process in self.processes.values():
            for thread in process.threads.values():
                for lib in thread.libs.values():
                    yield lib


class ProcessScope(object):

    def __init__(self, pid):
        self.pid = pid
        self.name = ''
        self.event_count = 0
        self.threads = {}  # map from tid to ThreadScope

    def get_thread(self, tid, thread_name):
        thread = self.threads.get(tid)
        if not thread:
            thread = self.threads[tid] = ThreadScope(tid)
        thread.name = thread_name
        if self.pid == tid:
            self.name = thread_name
        return thread

    def get_sample_info(self, gen_addr_hit_map):
        result = {}
        result['pid'] = self.pid
        result['eventCount'] = self.event_count
        threads = sorted(self.threads.values(), key=lambda a: a.event_count, reverse=True)
        result['threads'] = [thread.get_sample_info(gen_addr_hit_map)
                             for thread in threads]
        return result


class ThreadScope(object):

    def __init__(self, tid):
        self.tid = tid
        self.name = ''
        self.event_count = 0
        self.sample_count = 0
        self.libs = {}  # map from lib_id to LibScope
        self.call_graph = CallNode(-1)
        self.reverse_call_graph = CallNode(-1)

    def add_callstack(self, event_count, callstack, build_addr_hit_map):
        """ callstack is a list of tuple (lib_id, func_id, addr).
            For each i > 0, callstack[i] calls callstack[i-1]."""
        hit_func_ids = set()
        for i, (lib_id, func_id, addr) in enumerate(callstack):
            # When a callstack contains recursive function, only add for each function once.
            if func_id in hit_func_ids:
                continue
            hit_func_ids.add(func_id)

            lib = self.libs.get(lib_id)
            if not lib:
                lib = self.libs[lib_id] = LibScope(lib_id)
            function = lib.get_function(func_id)
            function.subtree_event_count += event_count
            if i == 0:
                lib.event_count += event_count
                function.event_count += event_count
                function.sample_count += 1
            if build_addr_hit_map:
                function.build_addr_hit_map(addr, event_count if i == 0 else 0, event_count)

        # build call graph and reverse call graph
        node = self.call_graph
        for item in reversed(callstack):
            node = node.get_child(item[1])
        node.event_count += event_count
        node = self.reverse_call_graph
        for item in callstack:
            node = node.get_child(item[1])
        node.event_count += event_count

    def update_subtree_event_count(self):
        self.call_graph.update_subtree_event_count()
        self.reverse_call_graph.update_subtree_event_count()

    def limit_percents(self, min_func_limit, min_callchain_percent, hit_func_ids):
        for lib in self.libs.values():
            to_del_funcs = []
            for function in lib.functions.values():
                if function.subtree_event_count < min_func_limit:
                    to_del_funcs.append(function.func_id)
                else:
                    hit_func_ids.add(function.func_id)
            for func_id in to_del_funcs:
                del lib.functions[func_id]
        min_limit = min_callchain_percent * 0.01 * self.call_graph.subtree_event_count
        self.call_graph.cut_edge(min_limit, hit_func_ids)
        self.reverse_call_graph.cut_edge(min_limit, hit_func_ids)

    def get_sample_info(self, gen_addr_hit_map):
        result = {}
        result['tid'] = self.tid
        result['eventCount'] = self.event_count
        result['sampleCount'] = self.sample_count
        result['libs'] = [lib.gen_sample_info(gen_addr_hit_map)
                          for lib in self.libs.values()]
        result['g'] = self.call_graph.gen_sample_info()
        result['rg'] = self.reverse_call_graph.gen_sample_info()
        return result


class LibScope(object):

    def __init__(self, lib_id):
        self.lib_id = lib_id
        self.event_count = 0
        self.functions = {}  # map from func_id to FunctionScope.

    def get_function(self, func_id):
        function = self.functions.get(func_id)
        if not function:
            function = self.functions[func_id] = FunctionScope(func_id)
        return function

    def gen_sample_info(self, gen_addr_hit_map):
        result = {}
        result['libId'] = self.lib_id
        result['eventCount'] = self.event_count
        result['functions'] = [func.gen_sample_info(gen_addr_hit_map)
                               for func in self.functions.values()]
        return result


class FunctionScope(object):

    def __init__(self, func_id):
        self.func_id = func_id
        self.sample_count = 0
        self.event_count = 0
        self.subtree_event_count = 0
        self.addr_hit_map = None  # map from addr to [event_count, subtree_event_count].
        # map from (source_file_id, line) to [event_count, subtree_event_count].
        self.line_hit_map = None

    def build_addr_hit_map(self, addr, event_count, subtree_event_count):
        if self.addr_hit_map is None:
            self.addr_hit_map = {}
        count_info = self.addr_hit_map.get(addr)
        if count_info is None:
            self.addr_hit_map[addr] = [event_count, subtree_event_count]
        else:
            count_info[0] += event_count
            count_info[1] += subtree_event_count

    def build_line_hit_map(self, source_file_id, line, event_count, subtree_event_count):
        if self.line_hit_map is None:
            self.line_hit_map = {}
        key = (source_file_id, line)
        count_info = self.line_hit_map.get(key)
        if count_info is None:
            self.line_hit_map[key] = [event_count, subtree_event_count]
        else:
            count_info[0] += event_count
            count_info[1] += subtree_event_count

    def gen_sample_info(self, gen_addr_hit_map):
        result = {}
        result['f'] = self.func_id
        result['c'] = [self.sample_count, self.event_count, self.subtree_event_count]
        if self.line_hit_map:
            items = []
            for key in self.line_hit_map:
                count_info = self.line_hit_map[key]
                item = {'f': key[0], 'l': key[1], 'e': count_info[0], 's': count_info[1]}
                items.append(item)
            result['s'] = items
        if gen_addr_hit_map and self.addr_hit_map:
            items = []
            for addr in sorted(self.addr_hit_map):
                count_info = self.addr_hit_map[addr]
                items.append({'a': addr, 'e': count_info[0], 's': count_info[1]})
            result['a'] = items
        return result


class CallNode(object):

    def __init__(self, func_id):
        self.event_count = 0
        self.subtree_event_count = 0
        self.func_id = func_id
        self.children = collections.OrderedDict()  # map from func_id to CallNode

    def get_child(self, func_id):
        child = self.children.get(func_id)
        if not child:
            child = self.children[func_id] = CallNode(func_id)
        return child

    def update_subtree_event_count(self):
        self.subtree_event_count = self.event_count
        for child in self.children.values():
            self.subtree_event_count += child.update_subtree_event_count()
        return self.subtree_event_count

    def cut_edge(self, min_limit, hit_func_ids):
        hit_func_ids.add(self.func_id)
        to_del_children = []
        for key in self.children:
            child = self.children[key]
            if child.subtree_event_count < min_limit:
                to_del_children.append(key)
            else:
                child.cut_edge(min_limit, hit_func_ids)
        for key in to_del_children:
            del self.children[key]

    def gen_sample_info(self):
        result = {}
        result['e'] = self.event_count
        result['s'] = self.subtree_event_count
        result['f'] = self.func_id
        result['c'] = [child.gen_sample_info() for child in self.children.values()]
        return result


class LibSet(object):
    """ Collection of shared libraries used in perf.data. """
    def __init__(self):
        self.lib_name_to_id = {}
        self.lib_id_to_name = []

    def get_lib_id(self, lib_name):
        lib_id = self.lib_name_to_id.get(lib_name)
        if lib_id is None:
            lib_id = len(self.lib_id_to_name)
            self.lib_name_to_id[lib_name] = lib_id
            self.lib_id_to_name.append(lib_name)
        return lib_id

    def get_lib_name(self, lib_id):
        return self.lib_id_to_name[lib_id]


class Function(object):
    """ Represent a function in a shared library. """
    def __init__(self, lib_id, func_name, func_id, start_addr, addr_len):
        self.lib_id = lib_id
        self.func_name = func_name
        self.func_id = func_id
        self.start_addr = start_addr
        self.addr_len = addr_len
        self.source_info = None
        self.disassembly = None


class FunctionSet(object):
    """ Collection of functions used in perf.data. """
    def __init__(self):
        self.name_to_func = {}
        self.id_to_func = {}

    def get_func_id(self, lib_id, symbol):
        key = (lib_id, symbol.symbol_name)
        function = self.name_to_func.get(key)
        if function is None:
            func_id = len(self.id_to_func)
            function = Function(lib_id, symbol.symbol_name, func_id, symbol.symbol_addr,
                                symbol.symbol_len)
            self.name_to_func[key] = function
            self.id_to_func[func_id] = function
        return function.func_id

    def trim_functions(self, left_func_ids):
        """ Remove functions excepts those in left_func_ids. """
        for function in self.name_to_func.values():
            if function.func_id not in left_func_ids:
                del self.id_to_func[function.func_id]
        # name_to_func will not be used.
        self.name_to_func = None


class SourceFile(object):
    """ A source file containing source code hit by samples. """
    def __init__(self, file_id, abstract_path):
        self.file_id = file_id
        self.abstract_path = abstract_path  # path reported by addr2line
        self.real_path = None  # file path in the file system
        self.requested_lines = set()
        self.line_to_code = {}  # map from line to code in that line.

    def request_lines(self, start_line, end_line):
        self.requested_lines |= set(range(start_line, end_line + 1))

    def add_source_code(self, real_path):
        self.real_path = real_path
        with open(real_path, 'r') as f:
            source_code = f.readlines()
        max_line = len(source_code)
        for line in self.requested_lines:
            if line > 0 and line <= max_line:
                self.line_to_code[line] = source_code[line - 1]
        # requested_lines is no longer used.
        self.requested_lines = None


class SourceFileSet(object):
    """ Collection of source files. """
    def __init__(self):
        self.path_to_source_files = {}  # map from file path to SourceFile.

    def get_source_file(self, file_path):
        source_file = self.path_to_source_files.get(file_path)
        if source_file is None:
            source_file = SourceFile(len(self.path_to_source_files), file_path)
            self.path_to_source_files[file_path] = source_file
        return source_file

    def load_source_code(self, source_dirs):
        file_searcher = SourceFileSearcher(source_dirs)
        for source_file in self.path_to_source_files.values():
            real_path = file_searcher.get_real_path(source_file.abstract_path)
            if real_path:
                source_file.add_source_code(real_path)



class RecordData(object):

    """RecordData reads perf.data, and generates data used by report.js in json format.
        All generated items are listed as below:
            1. recordTime: string
            2. machineType: string
            3. androidVersion: string
            4. recordCmdline: string
            5. totalSamples: int
            6. processNames: map from pid to processName.
            7. threadNames: map from tid to threadName.
            8. libList: an array of libNames, indexed by libId.
            9. functionMap: map from functionId to funcData.
                funcData = {
                    l: libId
                    f: functionName
                    s: [sourceFileId, startLine, endLine] [optional]
                    d: [(disassembly, addr)] [optional]
                }

            10.  sampleInfo = [eventInfo]
                eventInfo = {
                    eventName
                    eventCount
                    processes: [processInfo]
                }
                processInfo = {
                    pid
                    eventCount
                    threads: [threadInfo]
                }
                threadInfo = {
                    tid
                    eventCount
                    sampleCount
                    libs: [libInfo],
                    g: callGraph,
                    rg: reverseCallgraph
                }
                libInfo = {
                    libId,
                    eventCount,
                    functions: [funcInfo]
                }
                funcInfo = {
                    f: functionId
                    c: [sampleCount, eventCount, subTreeEventCount]
                    s: [sourceCodeInfo] [optional]
                    a: [addrInfo] (sorted by addrInfo.addr) [optional]
                }
                callGraph and reverseCallGraph are both of type CallNode.
                callGraph shows how a function calls other functions.
                reverseCallGraph shows how a function is called by other functions.
                CallNode {
                    e: selfEventCount
                    s: subTreeEventCount
                    f: functionId
                    c: [CallNode] # children
                }

                sourceCodeInfo {
                    f: sourceFileId
                    l: line
                    e: eventCount
                    s: subtreeEventCount
                }

                addrInfo {
                    a: addr
                    e: eventCount
                    s: subtreeEventCount
                }

            11. sourceFiles: an array of sourceFile, indexed by sourceFileId.
                sourceFile {
                    path
                    code:  # a map from line to code for that line.
                }
    """

    def __init__(self, binary_cache_path, ndk_path, build_addr_hit_map):
        self.binary_cache_path = binary_cache_path
        self.ndk_path = ndk_path
        self.build_addr_hit_map = build_addr_hit_map
        self.meta_info = None
        self.cmdline = None
        self.arch = None
        self.events = {}
        self.libs = LibSet()
        self.functions = FunctionSet()
        self.total_samples = 0
        self.source_files = SourceFileSet()
        self.gen_addr_hit_map_in_record_info = False

    def load_record_file(self, record_file, show_art_frames):
        lib = ReportLib()
        lib.SetRecordFile(record_file)
        # If not showing ip for unknown symbols, the percent of the unknown symbol may be
        # accumulated to very big, and ranks first in the sample table.
        lib.ShowIpForUnknownSymbol()
        if show_art_frames:
            lib.ShowArtFrames()
        if self.binary_cache_path:
            lib.SetSymfs(self.binary_cache_path)
        self.meta_info = lib.MetaInfo()
        self.cmdline = lib.GetRecordCmd()
        self.arch = lib.GetArch()
        while True:
            raw_sample = lib.GetNextSample()
            if not raw_sample:
                lib.Close()
                break
            raw_event = lib.GetEventOfCurrentSample()
            symbol = lib.GetSymbolOfCurrentSample()
            callchain = lib.GetCallChainOfCurrentSample()
            event = self._get_event(raw_event.name)
            self.total_samples += 1
            event.sample_count += 1
            event.event_count += raw_sample.period
            process = event.get_process(raw_sample.pid)
            process.event_count += raw_sample.period
            thread = process.get_thread(raw_sample.tid, raw_sample.thread_comm)
            thread.event_count += raw_sample.period
            thread.sample_count += 1

            lib_id = self.libs.get_lib_id(symbol.dso_name)
            func_id = self.functions.get_func_id(lib_id, symbol)
            callstack = [(lib_id, func_id, symbol.vaddr_in_file)]
            for i in range(callchain.nr):
                symbol = callchain.entries[i].symbol
                lib_id = self.libs.get_lib_id(symbol.dso_name)
                func_id = self.functions.get_func_id(lib_id, symbol)
                callstack.append((lib_id, func_id, symbol.vaddr_in_file))
            if len(callstack) > MAX_CALLSTACK_LENGTH:
                callstack = callstack[:MAX_CALLSTACK_LENGTH]
            thread.add_callstack(raw_sample.period, callstack, self.build_addr_hit_map)

        for event in self.events.values():
            for thread in event.threads:
                thread.update_subtree_event_count()

    def limit_percents(self, min_func_percent, min_callchain_percent):
        hit_func_ids = set()
        for event in self.events.values():
            min_limit = event.event_count * min_func_percent * 0.01
            for process in event.processes.values():
                to_del_threads = []
                for thread in process.threads.values():
                    if thread.call_graph.subtree_event_count < min_limit:
                        to_del_threads.append(thread.tid)
                    else:
                        thread.limit_percents(min_limit, min_callchain_percent, hit_func_ids)
                for thread in to_del_threads:
                    del process.threads[thread]
        self.functions.trim_functions(hit_func_ids)

    def _get_event(self, event_name):
        if event_name not in self.events:
            self.events[event_name] = EventScope(event_name)
        return self.events[event_name]

    def add_source_code(self, source_dirs, filter_lib):
        """ Collect source code information:
            1. Find line ranges for each function in FunctionSet.
            2. Find line for each addr in FunctionScope.addr_hit_map.
            3. Collect needed source code in SourceFileSet.
        """
        addr2line = Addr2Nearestline(self.ndk_path, self.binary_cache_path, False)
        # Request line range for each function.
        for function in self.functions.id_to_func.values():
            if function.func_name == 'unknown':
                continue
            lib_name = self.libs.get_lib_name(function.lib_id)
            if filter_lib(lib_name):
                addr2line.add_addr(lib_name, function.start_addr, function.start_addr)
                addr2line.add_addr(lib_name, function.start_addr,
                                   function.start_addr + function.addr_len - 1)
        # Request line for each addr in FunctionScope.addr_hit_map.
        for event in self.events.values():
            for lib in event.libraries:
                lib_name = self.libs.get_lib_name(lib.lib_id)
                if filter_lib(lib_name):
                    for function in lib.functions.values():
                        func_addr = self.functions.id_to_func[function.func_id].start_addr
                        for addr in function.addr_hit_map:
                            addr2line.add_addr(lib_name, func_addr, addr)
        addr2line.convert_addrs_to_lines()

        # Set line range for each function.
        for function in self.functions.id_to_func.values():
            if function.func_name == 'unknown':
                continue
            dso = addr2line.get_dso(self.libs.get_lib_name(function.lib_id))
            if not dso:
                continue
            start_source = addr2line.get_addr_source(dso, function.start_addr)
            end_source = addr2line.get_addr_source(dso, function.start_addr + function.addr_len - 1)
            if not start_source or not end_source:
                continue
            start_file_path, start_line = start_source[-1]
            end_file_path, end_line = end_source[-1]
            if start_file_path != end_file_path or start_line > end_line:
                continue
            source_file = self.source_files.get_source_file(start_file_path)
            source_file.request_lines(start_line, end_line)
            function.source_info = (source_file.file_id, start_line, end_line)

        # Build FunctionScope.line_hit_map.
        for event in self.events.values():
            for lib in event.libraries:
                dso = addr2line.get_dso(self.libs.get_lib_name(lib.lib_id))
                if not dso:
                    continue
                for function in lib.functions.values():
                    for addr in function.addr_hit_map:
                        source = addr2line.get_addr_source(dso, addr)
                        if not source:
                            continue
                        for file_path, line in source:
                            source_file = self.source_files.get_source_file(file_path)
                            # Show [line - 5, line + 5] of the line hit by a sample.
                            source_file.request_lines(line - 5, line + 5)
                            count_info = function.addr_hit_map[addr]
                            function.build_line_hit_map(source_file.file_id, line, count_info[0],
                                                        count_info[1])

        # Collect needed source code in SourceFileSet.
        self.source_files.load_source_code(source_dirs)

    def add_disassembly(self, filter_lib):
        """ Collect disassembly information:
            1. Use objdump to collect disassembly for each function in FunctionSet.
            2. Set flag to dump addr_hit_map when generating record info.
        """
        objdump = Objdump(self.ndk_path, self.binary_cache_path)
        cur_lib_name = None
        dso_info = None
        for function in sorted(self.functions.id_to_func.values(), key=lambda a: a.lib_id):
            if function.func_name == 'unknown':
                continue
            lib_name = self.libs.get_lib_name(function.lib_id)
            if lib_name != cur_lib_name:
                cur_lib_name = lib_name
                if filter_lib(lib_name):
                    dso_info = objdump.get_dso_info(lib_name)
                else:
                    dso_info = None
                if dso_info:
                    log_info('Disassemble %s' % dso_info[0])
            if dso_info:
                code = objdump.disassemble_code(dso_info, function.start_addr, function.addr_len)
                function.disassembly = code

        self.gen_addr_hit_map_in_record_info = True

    def gen_record_info(self):
        record_info = {}
        timestamp = self.meta_info.get('timestamp')
        if timestamp:
            t = datetime.datetime.fromtimestamp(int(timestamp))
        else:
            t = datetime.datetime.now()
        record_info['recordTime'] = t.strftime('%Y-%m-%d (%A) %H:%M:%S')

        product_props = self.meta_info.get('product_props')
        machine_type = self.arch
        if product_props:
            manufacturer, model, name = product_props.split(':')
            machine_type = '%s (%s) by %s, arch %s' % (model, name, manufacturer, self.arch)
        record_info['machineType'] = machine_type
        record_info['androidVersion'] = self.meta_info.get('android_version', '')
        record_info['recordCmdline'] = self.cmdline
        record_info['totalSamples'] = self.total_samples
        record_info['processNames'] = self._gen_process_names()
        record_info['threadNames'] = self._gen_thread_names()
        record_info['libList'] = self._gen_lib_list()
        record_info['functionMap'] = self._gen_function_map()
        record_info['sampleInfo'] = self._gen_sample_info()
        record_info['sourceFiles'] = self._gen_source_files()
        return record_info

    def _gen_process_names(self):
        process_names = {}
        for event in self.events.values():
            for process in event.processes.values():
                process_names[process.pid] = process.name
        return process_names

    def _gen_thread_names(self):
        thread_names = {}
        for event in self.events.values():
            for process in event.processes.values():
                for thread in process.threads.values():
                    thread_names[thread.tid] = thread.name
        return thread_names

    def _gen_lib_list(self):
        return [modify_text_for_html(x) for x in self.libs.lib_id_to_name]

    def _gen_function_map(self):
        func_map = {}
        for func_id in sorted(self.functions.id_to_func):
            function = self.functions.id_to_func[func_id]
            func_data = {}
            func_data['l'] = function.lib_id
            func_data['f'] = modify_text_for_html(function.func_name)
            if function.source_info:
                func_data['s'] = function.source_info
            if function.disassembly:
                disassembly_list = []
                for code, addr in function.disassembly:
                    disassembly_list.append([modify_text_for_html(code), addr])
                func_data['d'] = disassembly_list
            func_map[func_id] = func_data
        return func_map

    def _gen_sample_info(self):
        return [event.get_sample_info(self.gen_addr_hit_map_in_record_info)
                for event in self.events.values()]

    def _gen_source_files(self):
        source_files = sorted(self.source_files.path_to_source_files.values(),
                              key=lambda x: x.file_id)
        file_list = []
        for source_file in source_files:
            file_data = {}
            if not source_file.real_path:
                file_data['path'] = ''
                file_data['code'] = {}
            else:
                file_data['path'] = source_file.real_path
                code_map = {}
                for line in source_file.line_to_code:
                    code_map[line] = modify_text_for_html(source_file.line_to_code[line])
                file_data['code'] = code_map
            file_list.append(file_data)
        return file_list

URLS = {
    'jquery': 'https://ajax.googleapis.com/ajax/libs/jquery/3.3.1/jquery.min.js',
    'bootstrap4-css': 'https://stackpath.bootstrapcdn.com/bootstrap/4.1.2/css/bootstrap.min.css',
    'bootstrap4-popper':
        'https://cdnjs.cloudflare.com/ajax/libs/popper.js/1.12.9/umd/popper.min.js',
    'bootstrap4': 'https://stackpath.bootstrapcdn.com/bootstrap/4.1.2/js/bootstrap.min.js',
    'dataTable': 'https://cdn.datatables.net/1.10.19/js/jquery.dataTables.min.js',
    'dataTable-bootstrap4': 'https://cdn.datatables.net/1.10.19/js/dataTables.bootstrap4.min.js',
    'dataTable-css': 'https://cdn.datatables.net/1.10.19/css/dataTables.bootstrap4.min.css',
    'gstatic-charts': 'https://www.gstatic.com/charts/loader.js',
}

class ReportGenerator(object):

    def __init__(self, html_path):
        self.hw = HtmlWriter(html_path)
        self.hw.open_tag('html')
        self.hw.open_tag('head')
        for css in ['bootstrap4-css', 'dataTable-css']:
            self.hw.open_tag('link', rel='stylesheet', type='text/css', href=URLS[css]).close_tag()
        for js in ['jquery', 'bootstrap4-popper', 'bootstrap4', 'dataTable', 'dataTable-bootstrap4',
                   'gstatic-charts']:
            self.hw.open_tag('script', src=URLS[js]).close_tag()

        self.hw.open_tag('script').add(
            "google.charts.load('current', {'packages': ['corechart', 'table']});").close_tag()
        self.hw.open_tag('style', type='text/css').add("""
            .colForLine { width: 50px; }
            .colForCount { width: 100px; }
            .tableCell { font-size: 17px; }
            .boldTableCell { font-weight: bold; font-size: 17px; }
            """).close_tag()
        self.hw.close_tag('head')
        self.hw.open_tag('body')
        self.record_info = {}

    def write_content_div(self):
        self.hw.open_tag('div', id='report_content').close_tag()

    def write_record_data(self, record_data):
        self.hw.open_tag('script', id='record_data', type='application/json')
        self.hw.add(json.dumps(record_data))
        self.hw.close_tag()

    def write_script(self):
        self.hw.open_tag('script').add_file('report_html.js').close_tag()

    def finish(self):
        self.hw.close_tag('body')
        self.hw.close_tag('html')
        self.hw.close()


def main():
    sys.setrecursionlimit(MAX_CALLSTACK_LENGTH * 2 + 50)
    parser = argparse.ArgumentParser(description='report profiling data')
    parser.add_argument('-i', '--record_file', nargs='+', default=['perf.data'], help="""
                        Set profiling data file to report. Default is perf.data.""")
    parser.add_argument('-o', '--report_path', default='report.html', help="""
                        Set output html file. Default is report.html.""")
    parser.add_argument('--min_func_percent', default=0.01, type=float, help="""
                        Set min percentage of functions shown in the report.
                        For example, when set to 0.01, only functions taking >= 0.01%% of total
                        event count are collected in the report. Default is 0.01.""")
    parser.add_argument('--min_callchain_percent', default=0.01, type=float, help="""
                        Set min percentage of callchains shown in the report.
                        It is used to limit nodes shown in the function flamegraph. For example,
                        when set to 0.01, only callchains taking >= 0.01%% of the event count of
                        the starting function are collected in the report. Default is 0.01.""")
    parser.add_argument('--add_source_code', action='store_true', help='Add source code.')
    parser.add_argument('--source_dirs', nargs='+', help='Source code directories.')
    parser.add_argument('--add_disassembly', action='store_true', help='Add disassembled code.')
    parser.add_argument('--binary_filter', nargs='+', help="""Annotate source code and disassembly
                        only for selected binaries.""")
    parser.add_argument('--ndk_path', nargs=1, help='Find tools in the ndk path.')
    parser.add_argument('--no_browser', action='store_true', help="Don't open report in browser.")
    parser.add_argument('--show_art_frames', action='store_true',
                        help='Show frames of internal methods in the ART Java interpreter.')
    args = parser.parse_args()

    # 1. Process args.
    binary_cache_path = 'binary_cache'
    if not os.path.isdir(binary_cache_path):
        if args.add_source_code or args.add_disassembly:
            log_exit("""binary_cache/ doesn't exist. Can't add source code or disassembled code
                        without collected binaries. Please run binary_cache_builder.py to
                        collect binaries for current profiling data, or run app_profiler.py
                        without -nb option.""")
        binary_cache_path = None

    if args.add_source_code and not args.source_dirs:
        log_exit('--source_dirs is needed to add source code.')
    build_addr_hit_map = args.add_source_code or args.add_disassembly
    ndk_path = None if not args.ndk_path else args.ndk_path[0]

    # 2. Produce record data.
    record_data = RecordData(binary_cache_path, ndk_path, build_addr_hit_map)
    for record_file in args.record_file:
        record_data.load_record_file(record_file, args.show_art_frames)
    record_data.limit_percents(args.min_func_percent, args.min_callchain_percent)

    def filter_lib(lib_name):
        if not args.binary_filter:
            return True
        for binary in args.binary_filter:
            if binary in lib_name:
                return True
        return False
    if args.add_source_code:
        record_data.add_source_code(args.source_dirs, filter_lib)
    if args.add_disassembly:
        record_data.add_disassembly(filter_lib)

    # 3. Generate report html.
    report_generator = ReportGenerator(args.report_path)
    report_generator.write_script()
    report_generator.write_content_div()
    report_generator.write_record_data(record_data.gen_record_info())
    report_generator.finish()

    if not args.no_browser:
        open_report_in_browser(args.report_path)
    log_info("Report generated at '%s'." % args.report_path)


if __name__ == '__main__':
    main()
