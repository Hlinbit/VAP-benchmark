#!/usr/bin/env python3

import os
import time
import json
import yaml
import random
import string
import logging
import threading
import statistics
from datetime import datetime
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Any, Optional
import concurrent.futures

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from kubernetes import client, config
from kubernetes.client.rest import ApiException

# Setup console and logging
console = Console()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class RequestMetric:
    """Request metrics"""
    timestamp: float
    response_time: float
    success: bool
    blocked: bool


class MetricsCollector:
    """Metrics collector - statistics every 10 seconds"""
    
    def __init__(self):
        self.metrics = deque(maxlen=100000)  # Keep last 100,000 requests
        self.lock = threading.Lock()
        self.collecting = False
        self.stats_history = []
    
    def record_request(self, response_time: float, success: bool, blocked: bool = False):
        """Record a single request"""
        metric = RequestMetric(
            timestamp=time.time(),
            response_time=response_time,
            success=success,
            blocked=blocked
        )
        
        with self.lock:
            self.metrics.append(metric)
    
    def start_collection(self):
        """Start collecting statistics"""
        self.collecting = True
        stats_thread = threading.Thread(target=self._collect_stats)
        stats_thread.daemon = True
        stats_thread.start()
    
    def stop_collection(self):
        """Stop collecting"""
        self.collecting = False
    
    def _collect_stats(self):
        """Collect statistics every 10 seconds"""
        while self.collecting:
            time.sleep(10)
            stats = self._calculate_window_stats(10)
            if stats:
                self.stats_history.append(stats)
                self._display_stats(stats)
    
    def _calculate_window_stats(self, window_seconds: int) -> Dict[str, Any]:
        """Calculate statistics within time window"""
        current_time = time.time()
        window_start = current_time - window_seconds
        
        with self.lock:
            # Get metrics within time window
            window_metrics = [
                m for m in self.metrics 
                if m.timestamp >= window_start
            ]
        
        if not window_metrics:
            return {}
        
        # Calculate statistics
        total_requests = len(window_metrics)
        successful_requests = len([m for m in window_metrics if m.success])
        blocked_requests = len([m for m in window_metrics if m.blocked])
        error_requests = total_requests - successful_requests - blocked_requests
        
        response_times = [m.response_time for m in window_metrics]
        
        return {
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'window_seconds': window_seconds,
            'total_requests': total_requests,
            'successful_requests': successful_requests,
            'blocked_requests': blocked_requests,
            'error_requests': error_requests,
            'qps': total_requests / window_seconds,
            'success_rate': (successful_requests / total_requests * 100) if total_requests > 0 else 0,
            'block_rate': (blocked_requests / total_requests * 100) if total_requests > 0 else 0,
            'avg_latency_ms': statistics.mean(response_times) * 1000 if response_times else 0,
            'p50_latency_ms': statistics.median(response_times) * 1000 if response_times else 0,
            'p95_latency_ms': statistics.quantiles(response_times, n=20)[18] * 1000 if len(response_times) > 20 else 0,
            'p99_latency_ms': statistics.quantiles(response_times, n=100)[98] * 1000 if len(response_times) > 100 else 0,
        }
    
    def _display_stats(self, stats: Dict[str, Any]):
        """Display statistics"""
        table = Table(title=f"Real-time statistics - {stats['timestamp']}")
        table.add_column("Metric", style="cyan", width=15)
        table.add_column("Value", style="magenta", width=15)
        
        table.add_row("QPS", f"{stats['qps']:.2f}")
        table.add_row("Total requests", str(stats['total_requests']))
        table.add_row("Success rate", f"{stats['success_rate']:.1f}%")
        table.add_row("Block rate", f"{stats['block_rate']:.1f}%")
        table.add_row("Avg latency", f"{stats['avg_latency_ms']:.2f}ms")
        table.add_row("P50 latency", f"{stats['p50_latency_ms']:.2f}ms")
        table.add_row("P95 latency", f"{stats['p95_latency_ms']:.2f}ms")
        table.add_row("P99 latency", f"{stats['p99_latency_ms']:.2f}ms")
        
        console.print(table)
    
    def get_summary_stats(self) -> Dict[str, Any]:
        """Get summary statistics"""
        if not self.stats_history:
            return {}
        
        total_qps = sum(s['qps'] for s in self.stats_history)
        total_requests = sum(s['total_requests'] for s in self.stats_history)
        total_blocked = sum(s['blocked_requests'] for s in self.stats_history)
        
        avg_latencies = [s['avg_latency_ms'] for s in self.stats_history if s['avg_latency_ms'] > 0]
        p95_latencies = [s['p95_latency_ms'] for s in self.stats_history if s['p95_latency_ms'] > 0]
        
        return {
            'total_windows': len(self.stats_history),
            'avg_qps': total_qps / len(self.stats_history),
            'total_requests': total_requests,
            'total_blocked': total_blocked,
            'overall_block_rate': (total_blocked / total_requests * 100) if total_requests > 0 else 0,
            'avg_latency_ms': statistics.mean(avg_latencies) if avg_latencies else 0,
            'avg_p95_latency_ms': statistics.mean(p95_latencies) if p95_latencies else 0,
        }


class K8sAdmissionTesterBase:
    """Kubernetes admission policy tester base class"""
    
    def __init__(self, namespace: str = "vap-test", kubeconfig_path: str = "~/.kube/config"):
        self.namespace = namespace
        self._load_k8s_config(kubeconfig_path)
        
        self.v1 = client.CoreV1Api()
        self.custom_objects = client.CustomObjectsApi()
        self.api_extensions = client.ApiextensionsV1Api()
        
        self.metrics = MetricsCollector()
        self.stop_event = threading.Event()
        
        # CRD definition
        self.crd_group = "admission.test.io"
        self.crd_version = "v1"
        self.crd_plural = "labelconfigs"
        self.crd_kind = "LabelConfig"
    
    def _load_k8s_config(self, kubeconfig_path: str):
        """Load Kubernetes configuration"""
        try:
            if kubeconfig_path:
                config.load_kube_config(config_file=kubeconfig_path)
            else:
                config.load_kube_config()
        except:
            config.load_incluster_config()
    
    def create_namespace(self):
        """Create test namespace"""
        try:
            ns = client.V1Namespace(
                metadata=client.V1ObjectMeta(
                    name=self.namespace,
                    labels={"name": self.namespace, "vap-test": "true"}
                )
            )
            self.v1.create_namespace(body=ns)
            console.print(f"[green]✓ Create namespace: {self.namespace}[/green]")
        except ApiException as e:
            if e.status != 409:
                raise
            console.print(f"[yellow]Namespace {self.namespace} already exists[/yellow]")
    
    def create_parameter_crd(self):
        """Create parameter definition CRD - subclass can override"""
        pass
    
    def create_validating_admission_policy(self):
        """Create ValidatingAdmissionPolicy - subclass must implement"""
        raise NotImplementedError("Subclass must implement create_validating_admission_policy method")
    
    def create_parameter_cr(self, name: str, require_labels: List[str]):
        """Create parameter CR - subclass can override"""
        pass
    
    def create_policy_binding(self, name: str, policy_name: str, param_ref_name: str = ""):
        """Create ValidatingAdmissionPolicyBinding - subclass must implement"""
        raise NotImplementedError("Subclass must implement create_policy_binding method")
    
    def create_policy_binding_with_selector(self, name: str, policy_name: str):
        """Create ValidatingAdmissionPolicyBinding with selector - subclass can override"""
        pass

    def generate_bindings_and_params(self, count: int, policy_name: str):
        """Generate PolicyBinding and parameter CR - subclass must implement"""
        raise NotImplementedError("Subclass must implement generate_bindings_and_params method")
    
    def generate_bindings_and_params_with_selector(self, count: int, policy_name: str):
        """Generate PolicyBinding and parameter CR with selector - subclass can override"""
        pass

    def generate_random_string(self, length: int = 8) -> str:
        """Generate random string"""
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))
    
    def create_violating_configmap(self, all_required_labels: set) -> bool:
        """Create ConfigMap that violates all policies"""
        name = f"violating-cm-{self.generate_random_string()}"
        
        # Intentionally not include any required labels to ensure violation of all policies
        labels = {
            "test-target": "true",  # Must be included, otherwise policy won't match
            "test-type": "violating",
            "generated-at": str(int(time.time())),
            "random-id": self.generate_random_string(6)
        }
        
        # Can add some optional labels, but not include any required labels
        optional_labels = ["category", "purpose", "created-by", "test-run"]
        for label in random.sample(optional_labels, random.randint(1, 3)):
            labels[label] = f"value-{self.generate_random_string(4)}"
        
        data = {
            "config.yaml": f"test: data-{self.generate_random_string()}",
            "timestamp": str(time.time())
        }
        if self.stop_event.is_set():
            return True
        try:
            metadata = client.V1ObjectMeta(name=name, labels=labels)
            body = client.V1ConfigMap(metadata=metadata, data=data)
            
            start_time = time.time()
            self.v1.create_namespaced_config_map(namespace=self.namespace, body=body)
            response_time = time.time() - start_time
            
            # If successfully created, it means policy didn't take effect (unexpected situation)
            self.metrics.record_request(response_time, success=True, blocked=False)
            return True
            
        except ApiException as e:
            response_time = time.time() - start_time
            
            if e.status == 422 or "denied request" in str(e).lower():
                # Expected policy blocking
                self.metrics.record_request(response_time, success=False, blocked=True)
                return False
            else:
                # Other errors
                self.metrics.record_request(response_time, success=False, blocked=False)
                logger.warning(f"Unexpected error creating ConfigMap: {e}")
                return False
    
    def run_load_test(self, duration: int, qps: int, bindings_info: List[Dict]):
        """Run load test"""
        console.print(Panel.fit(
            f"[bold blue]Start load test[/bold blue]\n"
            f"Duration: {duration}s, Target QPS: {qps}\n"
            f"Policy count: {len(bindings_info)}"
        ))
        
        # Collect all required labels
        all_required_labels = set()
        for binding in bindings_info:
            # Handle both parameterized and non-parameterized cases
            if 'require_labels' in binding:
                all_required_labels.update(binding['require_labels'])
        
        console.print(f"[yellow]All required labels: {sorted(all_required_labels)}[/yellow]")
        
        # Start metrics collection
        self.metrics.start_collection()
        
        # Calculate request interval
        request_interval = 1.0 / qps
        end_time = time.time() + duration
        request_count = 0
        should_finish = False
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(qps, 50)) as executor:
            futures = []
            
            console.print(f"[cyan]Start generating ConfigMaps that violate policies...[/cyan]")
            
            try:
                while time.time() < end_time and not should_finish:
                    # Submit request to thread pool
                    future = executor.submit(self.create_violating_configmap, all_required_labels)
                    futures.append((future, time.time()))
                    request_count += 1
                    
                    # Control request rate
                    time.sleep(1.0 / qps)
                    
                    # Show progress every 1000 requests
                    if request_count % 1000 == 0:
                        remaining_time = end_time - time.time()
                        console.print(f"[dim]Sent {request_count} requests, {remaining_time:.0f}s remaining[/dim]")
                
                # Wait for all requests to complete
                for future, submit_time in futures:
                    try:
                        future.result(timeout=10)
                    except Exception as e:
                        logger.warning(f"Request failed after {time.time() - submit_time:.2f}s: {e}")
                        
            except KeyboardInterrupt:
                console.print("\n[yellow]Test interrupted by user[/yellow]")
                self.stop_event.set()

                time.sleep(1)
                executor.shutdown(wait=False)
                should_finish = True
            finally:
                self.metrics.stop_collection()

        
        console.print(f"[green]✓ Load test completed, sent {request_count} requests[/green]")
        
        # Display summary statistics
        summary = self.metrics.get_summary_stats()
        if summary:
            console.print("\n[bold cyan]Test Summary:[/bold cyan]")
            summary_table = Table()
            summary_table.add_column("Metric", style="cyan")
            summary_table.add_column("Value", style="magenta")
            
            summary_table.add_row("Statistics Periods", str(summary['total_windows']))
            summary_table.add_row("Average QPS", f"{summary['avg_qps']:.2f}")
            summary_table.add_row("Total Requests", str(summary['total_requests']))
            summary_table.add_row("Blocked Requests", str(summary['total_blocked']))
            summary_table.add_row("Overall Block Rate", f"{summary['overall_block_rate']:.1f}%")
            summary_table.add_row("Average Latency", f"{summary['avg_latency_ms']:.2f}ms")
            summary_table.add_row("Average P95 Latency", f"{summary['avg_p95_latency_ms']:.2f}ms")
            
            console.print(summary_table)
    
    def cleanup_resources(self):
        """Clean up test resources"""
        console.print("[yellow]Cleaning up test resources...[/yellow]")
        
        try:
            # Clean up ConfigMaps
            cms = self.v1.list_namespaced_config_map(namespace=self.namespace)
            for cm in cms.items:
                if cm.metadata.name.startswith("violating-cm-"):
                    self.v1.delete_namespaced_config_map(
                        name=cm.metadata.name, namespace=self.namespace
                    )
            
            # Clean up policy bindings
            bindings = self.custom_objects.list_cluster_custom_object(
                group="admissionregistration.k8s.io",
                version="v1",
                plural="validatingadmissionpolicybindings"
            )
            for binding in bindings.get('items', []):
                if binding['metadata']['name'].startswith('vap-binding-'):
                    self.custom_objects.delete_cluster_custom_object(
                        group="admissionregistration.k8s.io",
                        version="v1",
                        plural="validatingadmissionpolicybindings",
                        name=binding['metadata']['name']
                    )
            
            # Clean up parameter CRs
            params = self.custom_objects.list_namespaced_custom_object(
                group=self.crd_group,
                version=self.crd_version,
                namespace=self.namespace,
                plural=self.crd_plural
            )
            for param in params.get('items', []):
                if param['metadata']['name'].startswith('vap-param-'):
                    self.custom_objects.delete_namespaced_custom_object(
                        group=self.crd_group,
                        version=self.crd_version,
                        namespace=self.namespace,
                        plural=self.crd_plural,
                        name=param['metadata']['name']
                    )
            
            # Clean up policies
            policies = self.custom_objects.list_cluster_custom_object(
                group="admissionregistration.k8s.io",
                version="v1",
                plural="validatingadmissionpolicies"
            )
            for policy in policies.get('items', []):
                if policy['metadata']['name'] == 'parameterized-configmap-policy':
                    self.custom_objects.delete_cluster_custom_object(
                        group="admissionregistration.k8s.io",
                        version="v1",
                        plural="validatingadmissionpolicies",
                        name=policy['metadata']['name']
                    )
            
            console.print("[green]✓ Resource cleanup completed[/green]")
            
        except Exception as e:
            console.print(f"[red]Error during cleanup: {e}[/red]")


class K8sParameterizedAdmissionTester(K8sAdmissionTesterBase):
    """Parameterized admission policy tester"""
    
    def create_parameter_crd(self):
        """Create parameter definition CRD"""
        crd_manifest = {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {
                "name": f"{self.crd_plural}.{self.crd_group}"
            },
            "spec": {
                "group": self.crd_group,
                "versions": [{
                    "name": self.crd_version,
                    "served": True,
                    "storage": True,
                    "schema": {
                        "openAPIV3Schema": {
                            "type": "object",
                            "properties": {
                                "spec": {
                                    "type": "object",
                                    "properties": {
                                        "requireLabels": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "Required labels list for ConfigMap validation"
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "Description of this label configuration"
                                        }
                                    },
                                    "required": ["requireLabels"]
                                }
                            }
                        }
                    }
                }],
                "scope": "Namespaced",
                "names": {
                    "plural": self.crd_plural,
                    "singular": "labelconfig",
                    "kind": self.crd_kind
                }
            }
        }
        
        try:
            self.api_extensions.create_custom_resource_definition(body=crd_manifest)
            console.print(f"[green]✓ Create CRD: {self.crd_plural}.{self.crd_group}[/green]")
        except ApiException as e:
            if e.status != 409:
                raise
            console.print(f"[yellow]CRD already exists: {self.crd_plural}.{self.crd_group}[/yellow]")
        
        # Wait for CRD to be ready
        time.sleep(2)
    
    def create_validating_admission_policy(self):
        """Create parameterized ValidatingAdmissionPolicy"""
        policy_name = "parameterized-configmap-policy"
        
        # CEL expression: read requireLabels from parameters and check if ConfigMap contains these labels
        cel_expression = """
        params.spec.requireLabels.all(label, 
            has(object.metadata.labels) && label in object.metadata.labels
        )
        """
        
        policy_spec = {
            "failurePolicy": "Fail",
            "matchConstraints": {
                "resourceRules": [{
                    "operations": ["CREATE", "UPDATE"],
                    "apiGroups": [""],
                    "apiVersions": ["v1"],
                    "resources": ["configmaps"]
                }]
            },
            "paramKind": {
                "apiVersion": f"{self.crd_group}/{self.crd_version}",
                "kind": self.crd_kind
            },
            "validations": [{
                "expression": cel_expression.strip(),
                "message": "ConfigMap must contain all required labels specified in the binding parameters"
            }]
        }
        
        policy_body = {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicy",
            "metadata": {"name": policy_name},
            "spec": policy_spec
        }
        
        try:
            self.custom_objects.create_cluster_custom_object(
                group="admissionregistration.k8s.io",
                version="v1",
                plural="validatingadmissionpolicies",
                body=policy_body
            )
            console.print(f"[green]✓ Create ValidatingAdmissionPolicy: {policy_name}[/green]")
        except ApiException as e:
            if e.status != 409:
                raise
            console.print(f"[yellow]ValidatingAdmissionPolicy already exists: {policy_name}[/yellow]")
        
        return policy_name
    
    def create_parameter_cr(self, name: str, require_labels: List[str]) -> str:
        """Create parameter CR"""
        cr_body = {
            "apiVersion": f"{self.crd_group}/{self.crd_version}",
            "kind": self.crd_kind,
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": {"test-vap": "true"}
            },
            "spec": {
                "requireLabels": require_labels,
                "description": f"Label requirements for binding {name}"
            }
        }
        
        try:
            self.custom_objects.create_namespaced_custom_object(
                group=self.crd_group,
                version=self.crd_version,
                namespace=self.namespace,
                plural=self.crd_plural,
                body=cr_body
            )
            console.print(f"[green]✓ Create parameter CR: {name}[/green]")
        except ApiException as e:
            if e.status != 409:
                raise
            console.print(f"[yellow]Parameter CR already exists: {name}[/yellow]")
        
        return name
    
    def create_policy_binding(self, name: str, policy_name: str, param_ref_name: str):
        """Create ValidatingAdmissionPolicyBinding"""
        binding_spec = {
            "policyName": policy_name,
            "validationActions": ["Deny"],
            "matchResources": {
                "namespaceSelector": {
                    "matchLabels": {"vap-test": "true"}
                },
                "objectSelector": {
                    "matchLabels": {"test-target": "true"}
                }
            },
            "paramRef": {
                "name": param_ref_name,
                "namespace": self.namespace,
                "parameterNotFoundAction": "Allow"
            }
        }
        
        binding_body = {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": name},
            "spec": binding_spec
        }
        
        try:
            self.custom_objects.create_cluster_custom_object(
                group="admissionregistration.k8s.io",
                version="v1",
                plural="validatingadmissionpolicybindings",
                body=binding_body
            )
            console.print(f"[green]✓ Create PolicyBinding: {name}[/green]")
        except ApiException as e:
            if e.status != 409:
                raise
            console.print(f"[yellow]PolicyBinding already exists: {name}[/yellow]")
    
    def create_policy_binding_with_selector(self, name: str, policy_name: str):
        """Create ValidatingAdmissionPolicyBinding"""
        binding_spec = {
            "policyName": policy_name,
            "validationActions": ["Deny"],
            "matchResources": {
                "namespaceSelector": {
                    "matchLabels": {"vap-test": "true"}
                },
                "objectSelector": {
                    "matchLabels": {"test-target": "true"}
                }
            },
            "paramRef": {
                "selector": {
                    "matchLabels": {"test-vap": "true"}
                },
                "parameterNotFoundAction": "Allow"
            }
        }
        
        binding_body = {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": name},
            "spec": binding_spec
        }
        
        try:
            self.custom_objects.create_cluster_custom_object(
                group="admissionregistration.k8s.io",
                version="v1",
                plural="validatingadmissionpolicybindings",
                body=binding_body
            )
            console.print(f"[green]✓ Create PolicyBinding: {name}[/green]")
        except ApiException as e:
            if e.status != 409:
                raise
            console.print(f"[yellow]PolicyBinding already exists: {name}[/yellow]")

    def generate_bindings_and_params(self, count: int, policy_name: str):
        console.print(f"[cyan]Generate {count} PolicyBindings and parameter CRs...[/cyan]")
        
        # Predefined label pool
        available_labels = [
            "environment", "team", "application", "version", "tier",
            "component", "service", "owner", "project", "stage",
            "region", "zone", "cluster", "namespace", "release"
        ]
        
        bindings_info = []
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task("Creating bindings and parameters...", total=count)
            
            for i in range(count):
                binding_name = f"vap-binding-{i:03d}"
                param_name = f"vap-param-{i:03d}"
                
                # Generate different label requirements for each binding
                num_labels = random.randint(2, min(5, len(available_labels)))
                require_labels = random.sample(available_labels, num_labels)
                
                # Create parameter CR
                self.create_parameter_cr(param_name, require_labels)
                
                # Create binding
                self.create_policy_binding(binding_name, policy_name, param_name)
                
                bindings_info.append({
                    'binding_name': binding_name,
                    'param_name': param_name,
                    'require_labels': require_labels
                })
                
                progress.advance(task)
                time.sleep(0.1)  # Avoid API rate limiting
        
        return bindings_info
    
    def generate_bindings_and_params_with_selector(self, count: int, policy_name: str):
        console.print(f"[cyan]Generate {count} PolicyBindings and parameter CRs...[/cyan]")
        
        # Predefined label pool
        available_labels = [
            "environment", "team", "application", "version", "tier",
            "component", "service", "owner", "project", "stage",
            "region", "zone", "cluster", "namespace", "release"
        ]
        
        bindings_info = []
        binding_name = f"vap-binding-0"
        # Create binding
        self.create_policy_binding_with_selector(binding_name, policy_name)
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task("Creating bindings and parameters...", total=count)
            
            for i in range(count):
                param_name = f"vap-param-{i:03d}"
                
                # Generate different label requirements for each binding
                num_labels = random.randint(2, min(5, len(available_labels)))
                require_labels = random.sample(available_labels, num_labels)
                
                # Create parameter CR
                self.create_parameter_cr(param_name, require_labels)
                
                bindings_info.append({
                    'binding_name': binding_name,
                    'param_name': param_name,
                    'require_labels': require_labels
                })
                
                progress.advance(task)
                time.sleep(0.1)  # Avoid API rate limiting
        
        return bindings_info


class K8sNoneParameterizedAdmissionTester(K8sAdmissionTesterBase):
    """Non-parameterized admission policy tester"""
    
    def create_validating_admission_policy(self):
        """Create non-parameterized ValidatingAdmissionPolicy"""
        policy_name = "parameterized-configmap-policy"
        
        # CEL expression: hard-coded required labels
        cel_expression = """
        [ "environment", "team", "application", "version", "tier",
            "component", "service", "owner", "project", "stage",
            "region", "zone", "cluster", "namespace", "release"].all(label, 
            has(object.metadata.labels) && label in object.metadata.labels
        )
        """
        
        policy_spec = {
            "failurePolicy": "Fail",
            "matchConstraints": {
                "resourceRules": [{
                    "operations": ["CREATE", "UPDATE"],
                    "apiGroups": [""],
                    "apiVersions": ["v1"],
                    "resources": ["configmaps"]
                }]
            },
            "validations": [{
                "expression": cel_expression.strip(),
                "message": "ConfigMap must contain all required labels"
            }]
        }
        
        policy_body = {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicy",
            "metadata": {"name": policy_name},
            "spec": policy_spec
        }
        
        try:
            self.custom_objects.create_cluster_custom_object(
                group="admissionregistration.k8s.io",
                version="v1",
                plural="validatingadmissionpolicies",
                body=policy_body
            )
            console.print(f"[green]✓ Create ValidatingAdmissionPolicy: {policy_name}[/green]")
        except ApiException as e:
            if e.status != 409:
                raise
            console.print(f"[yellow]ValidatingAdmissionPolicy already exists: {policy_name}[/yellow]")
        
        return policy_name
    
    def create_policy_binding(self, name: str, policy_name: str, param_ref_name: str = ""):
        """Create ValidatingAdmissionPolicyBinding (no parameters)"""
        binding_spec = {
            "policyName": policy_name,
            "validationActions": ["Deny"],
            "matchResources": {
                "namespaceSelector": {
                    "matchLabels": {"vap-test": "true"}
                },
                "objectSelector": {
                    "matchLabels": {"test-target": "true"}
                }
            },
        }
        
        binding_body = {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": name},
            "spec": binding_spec
        }
        
        try:
            self.custom_objects.create_cluster_custom_object(
                group="admissionregistration.k8s.io",
                version="v1",
                plural="validatingadmissionpolicybindings",
                body=binding_body
            )
            console.print(f"[green]✓ Create PolicyBinding: {name}[/green]")
        except ApiException as e:
            if e.status != 409:
                raise
            console.print(f"[yellow]PolicyBinding already exists: {name}[/yellow]")
    
    def generate_bindings_and_params(self, count: int, policy_name: str):
        console.print(f"[cyan]Generate {count} PolicyBindings...[/cyan]")
        
        # Predefined label pool (for record only)
        available_labels = [
            "environment", "team", "application", "version", "tier",
            "component", "service", "owner", "project", "stage",
            "region", "zone", "cluster", "namespace", "release"
        ]
        
        bindings_info = []
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task("Creating bindings...", total=count)
            
            for i in range(count):
                binding_name = f"vap-binding-{i:03d}"
                
                # Generate different label requirements for each binding (for record only)
                num_labels = random.randint(2, min(5, len(available_labels)))
                require_labels = random.sample(available_labels, num_labels)
    
                # Create binding
                self.create_policy_binding(binding_name, policy_name)
                
                bindings_info.append({
                    'binding_name': binding_name,
                    'require_labels': require_labels
                })
                
                progress.advance(task)
                time.sleep(0.01)  # Avoid API rate limiting
        
        return bindings_info

    def generate_bindings_and_params_with_selector(self, count: int, policy_name: str):
        return self.generate_bindings_and_params(count, policy_name)


@click.command()
@click.option('--bindings', '-b', default=5, help='Number of ValidatingAdmissionPolicyBindings')
@click.option('--duration', '-d', default=30, help='Test duration (seconds)')
@click.option('--qps', '-q', default=10, help='Requests per second')
@click.option('--namespace', '-n', default='vap-test', help='Test namespace')
@click.option('--kubeconfig', '-k', default='', help='kubeconfig file path')
@click.option('--setup-only', is_flag=True, help='Setup environment only, do not run test')
@click.option('--cleanup-only', is_flag=True, help='Clean up resources only')
@click.option('--has-params', is_flag=True, help='Test with parameters')
@click.option('--use-selector', is_flag=True, help='use selector to fetch params')
@click.option('--no-cleanup', is_flag=True, help='Do not clean up resources after test')
def main(bindings, duration, qps, namespace, kubeconfig, setup_only, cleanup_only, has_params, use_selector, no_cleanup):
    """
    Kubernetes ValidatingAdmissionPolicy Parameterized Load Testing Tool
    
    This tool will:
    1. Create parameterized ValidatingAdmissionPolicy
    2. Create CRD to define policy parameters
    3. Generate specified number of bindings and parameter CRs
    4. Generate ConfigMaps that violate all policies for load testing
    5. Statistics latency and QPS every 10 seconds
    """
    
    console.print(Panel.fit(
        "[bold cyan]Kubernetes ValidatingAdmissionPolicy Parameterized Load Testing Tool[/bold cyan]\n"
        "[yellow]Test admission policy performance under high load[/yellow]"
    ))
    
    # Display configuration
    config_table = Table(title="Test Configuration")
    config_table.add_column("Parameter", style="cyan")
    config_table.add_column("Value", style="magenta")
    
    config_table.add_row("Binding Count", str(bindings))
    config_table.add_row("Test Duration", f"{duration}s")
    config_table.add_row("Target QPS", str(qps))
    config_table.add_row("Namespace", namespace)
    
    console.print(config_table)
    
    # Create tester
    if has_params:
        tester = K8sParameterizedAdmissionTester(namespace, kubeconfig)
    else: 
        tester = K8sNoneParameterizedAdmissionTester(namespace, kubeconfig)
    
    try:
        if cleanup_only:
            tester.cleanup_resources()
            return
        
        # Setup environment
        console.print("\n[bold green]== Step 1: Setup Test Environment ==[/bold green]")
        tester.create_namespace()
        tester.create_parameter_crd()
        
        console.print("\n[bold green]== Step 2: Create ValidatingAdmissionPolicy ==[/bold green]")
        policy_name = tester.create_validating_admission_policy()
        
        console.print("\n[bold green]== Step 3: Generate PolicyBindings and Parameter CRs ==[/bold green]")
        if use_selector:
            bindings_info = tester.generate_bindings_and_params_with_selector(bindings, policy_name)
        else:
            bindings_info = tester.generate_bindings_and_params(bindings, policy_name)
    
        console.print(f"\n[green]✓ Successfully created {len(bindings_info)} PolicyBindings and parameter CRs[/green]")
        
        # Wait for policies to take effect
        console.print("\n[yellow]Waiting for policies to take effect (3 seconds)...[/yellow]")
        time.sleep(3)
        
        if setup_only:
            console.print("[bold green]✓ Environment setup completed! Use --cleanup-only to clean up resources[/bold green]")
            return
        
        # Run load test
        console.print("\n[bold green]== Step 4: Run Load Test ==[/bold green]")
        tester.run_load_test(duration, qps, bindings_info)
        
        console.print("\n[bold green]✓ Test completed![/bold green]")
        
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Test interrupted by user[/bold yellow]")
    except Exception as e:
        console.print(f"\n[bold red]Test failed: {e}[/bold red]")
        import traceback
        console.print(traceback.format_exc())
    finally:
        if not no_cleanup and not setup_only:
            tester.cleanup_resources()


if __name__ == "__main__":
    main()