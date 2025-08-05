# Kubernetes ValidatingAdmissionPolicy Benchmark Tool

This tool benchmarks the performance of Kubernetes ValidatingAdmissionPolicy under load. It supports both parameterized and non-parameterized policies, generating various load scenarios to test policy evaluation performance.

## Features

- Test both parameterized and non-parameterized ValidatingAdmissionPolicy
- Generate configurable number of policy bindings
- Create load with configurable QPS (queries per second)
- Real-time metrics collection and display
- Automatic resource cleanup
- Support for parameter selection via label selectors

### Kubernetes Requirements

- Kubernetes cluster with ValidatingAdmissionPolicy feature enabled
- ValidatingAdmissionPolicy feature gate enabled
- kubeconfig file with sufficient permissions to:
  - Create/delete namespaces
  - Manage CustomResourceDefinitions
  - Manage ValidatingAdmissionPolicies
  - Manage ValidatingAdmissionPolicyBindings
  - Create/delete ConfigMaps in test namespace

## Setup a local kind cluster
Recommended kind version: 0.29.0
```
kind create cluster --config=kind-config/config.yaml 
```

## Installation

Recommended python version: 3.10.*

```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```



## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--bindings`, `-b` | 5 | Number of ValidatingAdmissionPolicyBindings |
| `--duration`, `-d` | 30 | Test duration in seconds |
| `--qps`, `-q` | 10 | Requests per second |
| `--namespace`, `-n` | vap-test | Test namespace |
| `--kubeconfig`, `-k` | ~/.kube/config | kubeconfig file path |
| `--setup-only` | False | Setup environment only, do not run test |
| `--cleanup-only` | False | Clean up resources only |
| `--has-params` | False | Test with parameters |
| `--use-selector` | False | Use selector to fetch params |
| `--no-cleanup` | False | Do not clean up resources after test |

## Examples
- Test non-parameterized policy with 10 bindings for 60 seconds at 20 QPS:
```
python benchmark.py --bindings 10 --duration 60 --qps 20
```
- est parameterized policy with 20 bindings for 120 seconds at 50 QPS:
```
python benchmark.py --has-params --bindings 20 --duration 120 --qps 50
```

- Test parameterized policy with selector-based parameter reference:
```
python benchmark.py --has-params --use-selector --bindings 15 --duration 90 --qps 30
```

- Clean up resources only:
```
python benchmark.py --cleanup-only
```