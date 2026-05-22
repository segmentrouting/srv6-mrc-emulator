# Makefile for srv6_mrc
#
# Convention: every target operates on a single TOPO (default 4p-4x8).
# Override with `make TOPO=<name> <target>`; the topology directory is
# expected at topologies/<TOPO>/ with a topo.yaml in it.
#
# Phases (run in order on first deploy):
#   make image      build the host container image (alpine + scapy + srv6_mrc)
#   make regen      regenerate topology.clab.yaml + SONiC configs from topo.yaml
#   make deploy     containerlab deploy
#   make config     push SONiC + FRR configs into running containers
#   make host-routes install host kernel routes (full-mesh by default)
#   make destroy   containerlab destroy
#
# Day-to-day:
#   make test       unit tests (~370 tests, ~1.5s)
#   make scenario SCEN=green-mrc-baseline   run an MRC scenario from topologies/<TOPO>/scenarios/
#   make help       this message

TOPO ?= 4p-4x8
TOPO_DIR := topologies/$(TOPO)
TOPO_YAML := $(TOPO_DIR)/topo.yaml
CLAB_YAML := $(TOPO_DIR)/topology.clab.yaml

# One image serves every topology. The active topology's topo.yaml is
# bind-mounted into each host container at runtime by containerlab
# (see generators/fabric.py and host-image/Dockerfile).
IMAGE_TAG ?= alpine-srv6-scapy:1.0

PYTHON ?= python3
PYTHONPATH := $(CURDIR)
export PYTHONPATH

# --- meta ------------------------------------------------------------------

.PHONY: help
help:
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
	      /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf "\nVariables:\n  TOPO=$(TOPO)   IMAGE_TAG=$(IMAGE_TAG)\n\n"

# --- testing ---------------------------------------------------------------

.PHONY: test
test: ## run the full unit test suite
	$(PYTHON) -m unittest discover -s tests -t .

# --- image -----------------------------------------------------------------

.PHONY: image
image: ## build the host container image with srv6_mrc baked in
	docker build -f host-image/Dockerfile -t $(IMAGE_TAG) .

# --- generation ------------------------------------------------------------

.PHONY: regen
regen: ## regenerate topology.clab.yaml + SONiC configs from topo.yaml
	$(PYTHON) generators/fabric.py --topo $(TOPO_YAML)

# --- lab lifecycle ---------------------------------------------------------

.PHONY: deploy
deploy: ## containerlab deploy the topology
	cd $(TOPO_DIR) && sudo containerlab deploy -t topology.clab.yaml

.PHONY: destroy
destroy: ## containerlab destroy the topology
	cd $(TOPO_DIR) && sudo containerlab destroy -t topology.clab.yaml --cleanup

.PHONY: config
config: ## push config_db.json + frr.conf into the running containers
	TOPO_DIR=$(CURDIR)/$(TOPO_DIR) scripts/config.sh all

.PHONY: verify-config
verify-config: ## verify SRv6 SIDs are programmed on every leaf; re-push failures
	TOPO_DIR=$(CURDIR)/$(TOPO_DIR) scripts/config.sh verify

# --- routes ----------------------------------------------------------------

ROUTES ?= full-mesh

.PHONY: host-routes
host-routes: ## install host kernel routes (override ROUTES=<name>, default full-mesh)
	SRV6_TOPO=$(TOPO_YAML) $(PYTHON) -m srv6_mrc.cli.routes apply -f $(TOPO_DIR)/routes/$(ROUTES).yaml

# --- mrc scenarios ---------------------------------------------------------

SCEN ?= green-mrc-baseline

.PHONY: scenario
scenario: ## run an MRC scenario (override SCEN=<name>, default green-mrc-baseline)
	SRV6_TOPO=$(TOPO_YAML) $(PYTHON) -m srv6_mrc.mrc.run $(TOPO_DIR)/scenarios/$(SCEN).yaml --verbose

# --- housekeeping ----------------------------------------------------------

.PHONY: clean
clean: ## remove caches and result artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf results/*.json
