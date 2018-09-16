from collections import defaultdict
from itertools import combinations, product

import numpy as np
from numpy.random import choice, random

from metal.label_model.graph_utils import DependenciesGraph, JunctionTree


# Dependencies Graphs
class TreeDependencies(DependenciesGraph):
    """Generate a random tree-structured dependency graph based on a
    specified edge probability.
    """

    def __init__(self, m, edge_prob=1.0):
        super().__init__(m)
        for i in range(1, m):
            if random() < edge_prob:
                self.G.add_edge(i, choice(i))


class ChainDependencies(DependenciesGraph):
    """Generate a chain-structured dependency graph."""

    def __init__(self, m, edge_prob=1.0):
        super().__init__(m)
        for i in range(1, m):
            if random() < edge_prob:
                p_i = i - 1
                self.G.add_edge(i, p_i)


class ClusterDependencies(DependenciesGraph):
    """Generate a cluster-structured dependency graph."""

    def __init__(self, m, n_clusters, edge_prob=1.0):
        super().__init__(m)
        self.clusters = defaultdict(set)
        for i in range(m):
            if random() < edge_prob:
                c = choice(n_clusters)
                for j in self.clusters[c]:
                    self.G.add_edge(i, j)
                self.clusters[c].add(i)


# DATA GENERATORS
class DataGenerator(object):
    """Generates a synthetic dataset

    Args:
        n: (int) The number of data points
        m: (int) The number of labeling sources
        k: (int) The cardinality of the classification task
        class_balance: (np.array) each class's percentage of the population
        deps_graph: (DependenciesGraph) A DependenciesGraph object
            specifying the dependencies structure of the sources
        param_ranges: (dict) A dictionary of ranges to draw the model parameters
            from:
            - theta_range_acc: (tuple) The min and max possible values for the
                class conditional accuracy for each labeling source
            - theta_range_edge: The min and max possible values for the strength
                of correlation between correlated sources

    Note that k = the # of true classes; thus source labels are in {0,1,...,k}
    because they include abstains.
    """

    def __init__(
        self,
        n,
        m,
        k=2,
        class_balance="random",
        deps_graph=None,
        param_ranges={
            "theta_range_acc": (0.1, 1),
            "theta_range_edge": (0.1, 1),
        },
        **kwargs,
    ):
        self.n = n
        self.m = m
        self.k = k

        # Form DependenciesGraph and JunctionTree
        self.deps_graph = (
            DependenciesGraph(m) if deps_graph is None else deps_graph
        )
        self.jt = JunctionTree(self.m, self.k, deps_graph=self.deps_graph)

        # Generate class-conditional LF & edge parameters, stored in self.theta
        self.theta = self._generate_params(param_ranges)

        # Generate class balance self.p
        if class_balance is None:
            self.p = np.full(k, 1 / k)
        elif class_balance == "random":
            self.p = np.random.random(k)
            self.p /= self.p.sum()
        else:
            self.p = class_balance

        # Cache for sum-product algorithm
        self.msg_cache = {}

        # Generate O, mu, Sigma, Sigma_inv
        # Y = (
        #     1
        # )  # Note: we pick an arbitrary Y here, since assuming doesn't matter
        # self.O = self._generate_O_Y(Y=Y)
        # self.mu = self._generate_mu_Y(Y=Y)
        # self.sig, self.sig_inv = self._generate_sigma(self.O, self.mu)

        # # Generate the true labels self.Y and label matrix self.L
        # self._generate_label_matrix()

    def _generate_params(self, param_ranges):
        """This function generates the parameters of the data generating model

        Note that along with the potential functions of the SPA algorithm, this
        essentially defines our model. This model is the most general form,
        where each marginal conditional probability for each clique C,
        P(\lf_C | Y), is generated randomly.
        """
        theta = defaultdict(float)

        # Unary clique factors
        # TODO: Set class balance here!

        # Binary clique factors
        for (i, j) in self.jt.deps_graph.G.edges():

            # Separate parameters for (\lf_i, Y) factors vs. (\lf_i, \lf_j)
            if i == self.m or j == self.m:
                theta_range = param_ranges["theta_range_acc"]
            else:
                theta_range = param_ranges["theta_range_edge"]
            t_min, t_max = min(theta_range), max(theta_range)

            for vals in product(range(self.k + 1), repeat=2):
                theta[((i, j), vals)] = (t_max - t_min) * random() + t_min
                theta[((j, i), vals[::-1])] = theta[((i, j), vals)]
        return theta

    def _exp_model(self, vals):
        """Compute the exponential model for a set of variables and values
        assuming an Ising model (i.e. only edge or node factors)

        Args:
            - vals: (dict) A dictionary of (LF index, value) entries.
        """
        x = 0.0

        # Node factors
        for i, val in vals.items():
            x += self.theta[(i, val)]

        # Edge factors
        for (i, val_i), (j, val_j) in combinations(vals.items(), 2):
            x += self.theta[((i, j), (val_i, val_j))]
        return np.exp(x)

    def P_marginal(self, targets, condition_on={}):
        """Compute P(targets|condition_on) using the sum-product algorithm over
        the junction tree `self.jt`"""
        for i, vi in {**targets, **condition_on}.items():
            if i < 0 or i > self.m or vi < 0 or vi > self.k:
                raise ValueError(f"Error with input {{{i}:{vi}}}")

        # Identify the clique containing the targets
        # TODO: Is this step actually necessary?
        ci = np.argmax(
            [
                len(self.jt.G.node[ci]["members"].intersection(targets.keys()))
                for ci in self.jt.G.nodes()
            ]
        )

        # Run the sum-product algorithm recursively
        p = self._message({**targets, **condition_on}, ci)

        # Return normalized probability
        Z = sum(
            [
                self._message(
                    {**dict(zip(targets.keys(), vals)), **condition_on}, ci
                )
                for vals in product(range(self.k + 1), repeat=len(targets))
            ]
        )
        return p / Z

    def _message(self, targets, i, j=None):
        """Computes the sum-product algorithm message from junction tree clique
        i --> j"""
        clique_members = self.jt.G.node[i]["members"]

        # Check the cache first, keyed by targets (projected onto members of
        # clique i), i, j
        cache_key = (
            i,
            j,
            tuple(
                [(ti, v) for ti, v in targets.items() if ti in clique_members]
            ),
        )
        if cache_key in self.msg_cache:
            return self.msg_cache[cache_key]

        # Sum over the values of clique i not in the target set
        # Note that the target set will include the separator set values, so
        # these will not be summed over (as desired)
        msg = 0
        val_ranges = {}
        for ci in clique_members:
            val_ranges[ci] = (
                [targets[ci]] if ci in targets else range(self.k + 1)
            )

        for vals in product(*val_ranges.values()):
            vals_dict = dict(zip(clique_members, vals))

            # Compute the local message for current node i
            msg_v = self._exp_model(vals_dict)

            # Recursively compute the messages from children
            children = set(self.jt.G.neighbors(i))
            if j is not None:
                children -= {j}
            for c in children:
                msg_v *= self._message({**vals_dict, **targets}, c, i)
            msg += msg_v

        # Cache message and return
        self.msg_cache[cache_key] = msg
        return msg

    def P_marginal_brute_force(self, targets, condition_on={}):
        """Compute P(targets|condition_on)"""
        for i, vi in {**targets, **condition_on}.items():
            if i < 0 or i > self.m or vi < 0 or vi > self.k:
                raise ValueError(f"Error with input {{{i}:{vi}}}")

        non_target = (
            set(range(self.m + 1))
            - set(targets.keys())
            - set(condition_on.keys())
        )

        # The numerator has the target and condition_on variables fixed, and
        # sums over all the remaining vars
        p = sum(
            [
                self._exp_model(
                    {**targets, **condition_on, **dict(zip(non_target, vals))}
                )
                for vals in product(range(self.k + 1), repeat=len(non_target))
            ]
        )

        # The demoninator has only condition_on variables fixed, and sums over
        # all the remaining vars
        norm_vars = set(range(self.m + 1)) - set(condition_on.keys())
        Z = sum(
            [
                self._exp_model({**condition_on, **dict(zip(norm_vars, vals))})
                for vals in product(range(self.k + 1), repeat=len(norm_vars))
            ]
        )
        return p / Z

    def compute_sigma_O(self, higher_order=False):
        if higher_order:
            raise NotImplementedError()

        # Implementation for unary cliques
        d = self.m * self.k
        sigma_O = np.zeros((d, d))

        # E[\lambda \lambda^T] - E[\lambda] E[\lambda]^T entrywise
        for (i, j) in product(range(self.m), repeat=2):
            for (vi, vj) in product(range(self.k), repeat=2):
                sigma_O[i + vi, j + vj] = self.P_marginal(
                    {i: vi, j: vj}
                ) - self.P_marginal({i: vi}) * self.P_marginal({j: vj})

        return sigma_O
