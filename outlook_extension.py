 import os
import re
import logging
import time

from contextlib import contextmanager
from typing import List, Dict

from ruxit.api.base_plugin import BasePlugin
from ruxit.api.exceptions import ConfigException
from ruxit.api.selectors import ExplicitPgiSelector
from ruxit.api.data import PluginMeasurement

from plugin_utils import exception_logger, time_it

if os.name == "nt":
    import win32pdh


logger = logging.getLogger(__name__)


@contextmanager
def query_perfmon():
    perfmon_conn = win32pdh.OpenQuery()
    yield perfmon_conn
    win32pdh.CloseQuery(perfmon_conn)


class Instance:
    def __init__(self, name, counter_query):
        self.name = name
        self.value = 0
        self.counter_query = counter_query
        self.pid = None

    def __str__(self):
        return f"Instance {self.name}: {self.value})"


class Metric(object):
    def __init__(self, key, obj, counter, result_type, process_name):
        self.key = key
        self.obj = obj
        self.counter = counter
        self.instances = []
        self.result_type = result_type
        self.process_name = process_name

    def __str__(self):
        return f"Metric {self.obj}:{self.counter} ({len(self.instances)} instances)"


def get_perfmon_counters(metrics_json) -> List[Metric]:

    collected_metrics: List[Metric] = []
    with query_perfmon() as perfmon_conn:

        # A cache so we don't need to ask windows for the list of the instances multiple times
        instance_cache: Dict[str, List[str]] = {}

        perfmon_metrics = [metric for metric in metrics_json if "source" in metric]

        for metric_json in perfmon_metrics:

            result_type = "absolute"
            if "type" in metric_json["source"]:
                result_type = metric_json["source"]["type"]

            # This class will hold a list of it's instances
            metric = Metric(
                metric_json["timeseries"]["key"],
                metric_json["source"]["object"],
                metric_json["source"]["counter"],
                result_type,
                metric_json["source"]["process_name"],
            )

            if metric.obj not in instance_cache:
                _, instances = win32pdh.EnumObjectItems(
                    None, None, metric.obj, win32pdh.PERF_DETAIL_WIZARD
                )
            else:
                instances = instance_cache[metric.obj]

            if instances:
                for instance in instances:
                    try:
                        # Add the query to perfmon, this does not do any metric collection
                        path = win32pdh.MakeCounterPath(
                            (None, metric.obj, instance, None, -1, metric.counter)
                        )
                        metric.instances.append(
                            Instance(instance, win32pdh.AddCounter(perfmon_conn, path))
                        )
                    except Exception as e:
                        logger.exception(
                            f"Could not add the counter {metric.obj} - {metric.counter}: {e}"
                        )
            else:
                path = win32pdh.MakeCounterPath(
                    (None, metric.obj, None, None, -1, metric.counter)
                )
                metric.instances.append(
                    Instance("Null", win32pdh.AddCounter(perfmon_conn, path))
                )

            collected_metrics.append(metric)

        # We do this because of % and rate metrics need to be collected twice
        # This needs to be reviewed, we cannot collect a subset of datapoints for a 60 second interval
        # We cannot also keep the perfmon connection opened across multiple runs (maybe we can?)
        try:
            win32pdh.CollectQueryData(perfmon_conn)
            time.sleep(1)
            win32pdh.CollectQueryData(perfmon_conn)
        except Exception as e:
            logger.exception(f"Failed to collect query data: {e}")

        for metric in collected_metrics:
            for instance in metric.instances:
                _, value = win32pdh.GetFormattedCounterValue(instance.counter_query, win32pdh.PDH_FMT_DOUBLE)
                instance.value = value

    return collected_metrics


class OUTLOOKExtension(BasePlugin):
    def initialize(self, **kwargs):
        self.metrics = kwargs["json_config"]["metrics"]
        self.name = kwargs["json_config"]["name"]

    @exception_logger
    @time_it
    def query(self, **kwargs):

        if os.name != "nt":
            raise ConfigException("The plugin can only run on Windows hosts")

        config = kwargs["config"]
        self.monitored_instances = []
        self.ignored_instances = []

        if config.get("monitored", None):
            self.monitored_instances = config.get("monitored").split(",")

        if config.get("ignored", None):
            self.ignored_instances = config.get("ignored").split(",")

        self.snapshot_entries = kwargs["process_snapshot"]
        self.collect_metrics()

    @exception_logger
    @time_it
    def collect_metrics(self):
        for metric in get_perfmon_counters(self.metrics):
            for instance in metric.instances:

                if self.will_monitor(instance.name):
                    target_process = self.get_pid_from_name(metric.process_name)
                    logger.info(
                        f"{metric} - {instance} will be sent to process {target_process} "
                    )

                    self.send_metric(metric, instance, target_process)

    def send_metric(self, metric: Metric, instance: Instance, process):
        method = self.results_builder.add_absolute_result
        if metric.result_type == "relative":
            method = self.results_builder.add_relative_result

        method(
            PluginMeasurement(
                key=metric.key,
                value=instance.value,
                dimensions={
                    "Instance": f"{instance.name}",
                    "rx_pid": f'{process["pid"]:X}',
                },
                entity_selector=ExplicitPgiSelector(process["pgi"]),
            )
        )

    def get_pid_from_name(self, name):
        for entry in self.snapshot_entries.entries:
            for process in entry.processes:
                if name in process.process_name:
                    return {"pgi": entry.group_instance_id, "pid": process.pid}

        logger.warning(f"Could not find process {name}")

    def will_monitor(self, instance_name):
        for ignored in self.ignored_instances:
            if re.findall(ignored, instance_name):
                #self.logger.info(
                #    f"Instance '{instance_name}' will not be monitored, it matched the regex: '{ignored}'"
                #)
                return False

        if not self.monitored_instances:
            self.logger.info(f"Instance '{instance_name}' will be monitored")
            return True

        for monitored in self.monitored_instances:
            if re.findall(monitored, instance_name):
                self.logger.info(
                    f"Instance '{instance_name}' will be monitored, it matched the regex: '{monitored}'"
                )
                return True
        #self.logger.info(
        #    (
        #        f"Instance '{instance_name}' will not be monitored",
        #        f"(ignored: '{self.ignored_instances}', monitored: '{self.monitored_instances}')",
        #    )
        #)
        return False
