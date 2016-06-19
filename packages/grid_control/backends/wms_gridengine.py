# | Copyright 2009-2016 Karlsruhe Institute of Technology
# |
# | Licensed under the Apache License, Version 2.0 (the "License");
# | you may not use this file except in compliance with the License.
# | You may obtain a copy of the License at
# |
# |     http://www.apache.org/licenses/LICENSE-2.0
# |
# | Unless required by applicable law or agreed to in writing, software
# | distributed under the License is distributed on an "AS IS" BASIS,
# | WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# | See the License for the specific language governing permissions and
# | limitations under the License.

import os, xml.dom.minidom
from grid_control import utils
from grid_control.backends.backend_tools import CheckInfo, CheckJobsViaArguments
from grid_control.backends.wms import BackendError, WMS
from grid_control.backends.wms_pbsge import PBSGECommon
from grid_control.config import ConfigError
from grid_control.job_db import Job
from grid_control.utils.parsing import parseTime
from grid_control.utils.process_base import LocalProcess
from python_compat import imap, izip, lmap, set

class GridEngine_CheckJobs(CheckJobsViaArguments):
	def __init__(self, config, user = None):
		CheckJobsViaArguments.__init__(self, config)
		self._user = user
		self._check_exec = utils.resolveInstallPath('qstat')

	def _arguments(self, wmsIDs):
		args = [self._check_exec, '-xml']
		if self._user:
			args.extend(['-u', self._user])
		return args

	def _parse(self, proc):
		proc.status(timeout = self._timeout)
		status_string = proc.stdout.read(timeout = 0)
		# qstat gives invalid xml in <unknown_jobs> node
		unknown_start = status_string.find('<unknown_jobs')
		unknown_jobs_string = ''
		if unknown_start >= 0:
			unknown_end_tag = '</unknown_jobs>'
			unknown_end = status_string.find(unknown_end_tag) + len(unknown_end_tag)
			unknown_jobs_string = status_string[unknown_start:unknown_end]
			unknown_jobs_string_fixed = unknown_jobs_string.replace('<>', '<unknown_job>').replace('</>', '</unknown_job>')
			status_string = status_string.replace(unknown_jobs_string, unknown_jobs_string_fixed)
		try:
			dom = xml.dom.minidom.parseString(status_string)
		except Exception:
			raise BackendError("Couldn't parse qstat XML output!")
		for job_node in dom.getElementsByTagName('job_list'):
			job_info = {}
			try:
				for node in job_node.childNodes:
					if node.nodeType != xml.dom.minidom.Node.ELEMENT_NODE:
						continue
					if node.hasChildNodes():
						job_info[str(node.nodeName)] = str(node.childNodes[0].nodeValue)
				job_info[CheckInfo.WMSID] = job_info.pop('JB_job_number')
				job_info[CheckInfo.RAW_STATUS] = job_info.pop('state')
				if 'queue_name' in job_info:
					queue, node = job_info['queue_name'].split('@')
					job_info[CheckInfo.QUEUE] = queue
					job_info[CheckInfo.WN] = node
			except Exception:
				raise BackendError('Error reading job info:\n%s' % job_node.toxml())
			yield job_info

	def _parse_status(self, value, default):
		if True in imap(lambda x: x in value, ['E', 'e']):
			return Job.CANCEL
		if True in imap(lambda x: x in value, ['h', 's', 'S', 'T', 'w']):
			return Job.QUEUED
		if True in imap(lambda x: x in value, ['r', 't']):
			return Job.RUNNING
		return Job.READY


class GridEngine(PBSGECommon):
	alias = ['SGE', 'UGE', 'OGE']
	configSections = PBSGECommon.configSections + ['GridEngine'] + alias

	def __init__(self, config, name):
		self._user = config.get('user', os.environ.get('LOGNAME', ''), onChange = None)
		PBSGECommon.__init__(self, config, name, checkExecutor = GridEngine_CheckJobs(config, self._user))
		self._project = config.get('project name', '', onChange = None)
		self._configExec = utils.resolveInstallPath('qconf')


	def getSubmitArguments(self, jobNum, jobName, reqs, sandbox, stdout, stderr):
		timeStr = lambda s: '%02d:%02d:%02d' % (s / 3600, (s / 60) % 60, s % 60)
		reqMap = { WMS.MEMORY: ('h_vmem', lambda m: '%dM' % m),
			WMS.WALLTIME: ('s_rt', timeStr), WMS.CPUTIME: ('h_cpu', timeStr) }
		# Restart jobs = no
		params = ' -r n -notify'
		if self._project:
			params += ' -P %s' % self._project
		# Job requirements
		(queue, nodes) = (reqs.get(WMS.QUEUES, [''])[0], reqs.get(WMS.SITES))
		if not nodes and queue:
			params += ' -q %s' % queue
		elif nodes and queue:
			params += ' -q %s' % str.join(',', imap(lambda node: '%s@%s' % (queue, node), nodes))
		elif nodes:
			raise ConfigError('Please also specify queue when selecting nodes!')
		return params + PBSGECommon.getCommonSubmitArguments(self, jobNum, jobName, reqs, sandbox, stdout, stderr, reqMap)


	def parseSubmitOutput(self, data):
		# Your job 424992 ("test.sh") has been submitted
		return data.split()[2].strip()


	def getCancelArguments(self, wmsIds):
		return str.join(',', wmsIds)


	def getQueues(self):
		queues = {}
		tags = ['h_vmem', 'h_cpu', 's_rt']
		reqs = dict(izip(tags, [WMS.MEMORY, WMS.CPUTIME, WMS.WALLTIME]))
		parser = dict(izip(tags, [int, parseTime, parseTime]))

		proc = LocalProcess(self._configExec, '-sql')
		for queue in imap(str.strip, proc.stdout.iter(timeout = 10)):
			queues[queue] = dict()
			proc_q = LocalProcess(self._configExec, '-sq %s' % queue)
			for line in proc_q.stdout.iter(timeout = 10):
				attr, value = lmap(str.strip, line.split(' ', 1))
				if (attr in tags) and (value != 'INFINITY'):
					queues[queue][reqs[attr]] = parser[attr](value)
			proc_q.status_raise(timeout = 0)
		proc.status_raise(timeout = 0)
		return queues


	def getNodes(self):
		result = set()
		proc = LocalProcess(self._configExec, '-shgrpl')
		for group in proc.stdout.iter(timeout = 10):
			result.add(group.strip())
			proc_g = LocalProcess(self._configExec, '-shgrp_resolved %s' % group)
			for host in proc_g.stdout.iter(timeout = 10):
				result.update(host.split())
			proc_g.status_raise(timeout = 0)
		proc.status_raise(timeout = 0)
		if len(result) > 0:
			return list(result)
