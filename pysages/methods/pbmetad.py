# SPDX-License-Identifier: MIT
# Copyright (c) 2020-2021: PySAGES contributors
# See LICENSE.md and CONTRIBUTORS.md at https://github.com/SSAGESLabs/PySAGES

"""
Implementation of Parallel Bias Well-tempered Metadynamics with optional support for grids.
"""

from typing import NamedTuple, Optional

from jax import numpy as np, grad, jit, value_and_grad, vmap
from jax.lax import cond

from pysages.approxfun import compute_mesh
from pysages.collective_variables import get_periods, wrap
from pysages.methods.core import Result, GriddedSamplingMethod, generalize
from pysages.utils import JaxArray, identity
from pysages.grids import build_indexer
from pysages.utils import dispatch
from pysages.methods.metad import MetadynamicsState, PartialMetadynamicsState


class ParallelBiasMetadynamics(GriddedSamplingMethod):
    """
    Implementation of Parallel Bias Metadynamics as described in
    [J. Chem. Theory Comput. 11, 5062–5067 (2015)](https://doi.org/10.1021/acs.jctc.5b00846)

    Compared to well-tempered metadynamics, the height of Gaussian bias deposited along
    each CV is different in parallel bias metadynamics. In addition, the total bias potential
    has a slightly different expression involving the sum of exponential of Gaussians (see Eq. 8)
    in the paper.
    """

    snapshot_flags = {"positions", "indices"}

    def __init__(self, cvs, height, sigma, stride, ngaussians, T, deltaT, kB, *args, **kwargs):
        """
        Arguments
        ---------

        cvs:
            Set of user selected collective variables.

        height:
            Initial height of the deposited Gaussians along each CV.

        sigma:
            Initial standard deviation of the to-be-deposit Gaussians along each CV.

        stride: int
            Bias potential deposition frequency.

        ngaussians: int
            Total number of expected gaussians (timesteps // stride + 1).

        deltaT: float = None
            Well-tempered metadynamics $\\Delta T$ parameter to set the energy
            scale for sampling.

        kB: float
            Boltzmann constant. It should match the internal units of the backend.

        Keyword arguments
        -----------------

        grid: Optional[Grid] = None
            If provided, it will be used to accelerate the computation by
            approximating the bias potential and its gradient over its centers.

        """

        kwargs["grid"] = kwargs.get("grid", None)
        super().__init__(cvs, **kwargs)

        self.height = height
        self.sigma = sigma
        self.stride = stride
        self.ngaussians = ngaussians  # NOTE: infer from timesteps and stride
        self.T = T
        self.deltaT = deltaT
        self.kB = kB

    def build(self, snapshot, helpers, *args, **kwargs):
        return _parallelbiasmetadynamics(self, snapshot, helpers)


def _parallelbiasmetadynamics(method, snapshot, helpers):
    # Initialization and update of biasing forces. Interface expected for methods.
    cv = method.cv
    stride = method.stride
    ngaussians = method.ngaussians
    natoms = np.size(snapshot.positions, 0)

    deposit_gaussian = build_gaussian_accumulator(method)
    evaluate_bias_grad = build_bias_grad_evaluator(method)

    def initialize():
        bias = np.zeros((natoms, 3), dtype=np.float64)
        xi, _ = cv(helpers.query(snapshot))

        # NOTE: for restart; use hills file to initialize corresponding arrays.
        heights = np.zeros((ngaussians, xi.size), dtype=np.float64)
        centers = np.zeros((ngaussians, xi.size), dtype=np.float64)
        sigmas = np.array(method.sigma, dtype=np.float64, ndmin=2)

        # Arrays to store forces and bias potential on a grid.
        if method.grid is None:
            grid_potential = grid_gradient = None
        else:
            shape = method.grid.shape
            grid_potential = np.zeros((*shape,), dtype=np.float64)
            grid_gradient = np.zeros((*shape, shape.size), dtype=np.float64)

        return MetadynamicsState(
            bias, xi, heights, centers, sigmas, grid_potential, grid_gradient, 0, 0
        )

    def update(state, data):
        # Compute the collective variable and its jacobian
        xi, Jxi = cv(data)

        # Deposit Gaussian depending on the stride
        nstep = state.nstep
        in_deposition_step = (nstep > 0) & (nstep % stride == 0)
        partial_state = deposit_gaussian(xi, state, in_deposition_step)

        # Evaluate gradient of biasing potential (or generalized force)
        generalized_force = evaluate_bias_grad(partial_state)

        # Calculate biasing forces
        bias = -Jxi.T @ generalized_force.flatten()
        bias = bias.reshape(state.bias.shape)

        return MetadynamicsState(bias, *partial_state[:-1], nstep + 1)

    return snapshot, initialize, generalize(update, helpers, jit_compile=False)


def build_gaussian_accumulator(method: ParallelBiasMetadynamics):
    """
    Returns a function that given a `MetadynamicsState`, and the value of the CV,
    stores the next Gaussian that is added to the biasing potential.
    """
    periods = get_periods(method.cvs)
    height_0 = np.array(method.height, dtype=np.float64)
    T = method.T
    deltaT = method.deltaT
    grid = method.grid
    kB = method.kB
    beta = 1 / (kB * T)
    kB_deltaT = kB * deltaT

    if grid is None:
        evaluate_potential_cv = jit(lambda pstate: get_bias_each_cv(*pstate[:4], periods))
        #evaluate_potential = jit(lambda pstate: parallelbias(*pstate[:4], beta, periods))
    #else:
    #    evaluate_potential = jit(lambda pstate: pstate.grid_potential[pstate.grid_idx])

    def next_height(pstate):
        V = evaluate_potential_cv(pstate)
        w = height_0 * np.exp(-V / kB_deltaT)
        switching_probability_sum = np.sum(np.exp(-beta * V))
        switching_probability = np.exp(-beta * V) / switching_probability_sum

        return w * switching_probability

    if grid is None:
        get_grid_index = jit(lambda arg: None)
        update_grids = jit(lambda *args: (None, None))
        should_deposit = jit(lambda pred, _: pred)

    #else:
    #    grid_mesh = compute_mesh(grid) * (grid.size / 2)
    #    get_grid_index = build_indexer(grid)
    #    # Reshape so the dimensions are compatible
    #    accum = jit(lambda total, val: total + val.reshape(total.shape))

    #    transform = value_and_grad
    #    pack = identity
    #    update = jit(lambda V, dV, vals, grads: (accum(V, vals), accum(dV, grads)))

    #    def update_grids(pstate, height, xi, sigma):
    #        # We use `sum_of_gaussians` since it already takes care of the wrapping
    #        current_gaussian = jit(lambda x: sum_of_gaussians(x, height, xi, sigma, periods))
    #        # Evaluate gradient of bias (and bias potential for WT version)
    #        grid_values = pack(vmap(transform(current_gaussian))(grid_mesh))
    #        return update(pstate.grid_potential, pstate.grid_gradient, *grid_values)

    #    def should_deposit(in_deposition_step, I_xi):
    #        in_bounds = ~(np.any(np.array(I_xi) == grid.shape))
    #        return in_deposition_step & in_bounds

    def deposit_gaussian(pstate):
        xi, idx = pstate.xi, pstate.idx
        current_height = next_height(pstate)
        heights = pstate.heights.at[idx].set(current_height.flatten())
        centers = pstate.centers.at[idx].set(xi.flatten())
        sigmas = pstate.sigmas
        grid_potential, grid_gradient = update_grids(pstate, current_height, xi, sigmas)
        return PartialMetadynamicsState(
            xi, heights, centers, sigmas, grid_potential, grid_gradient, idx + 1, pstate.grid_idx
        )

    def _deposit_gaussian(xi, state, in_deposition_step):
        I_xi = get_grid_index(xi)
        pstate = PartialMetadynamicsState(xi, *state[2:-1], I_xi)
        predicate = should_deposit(in_deposition_step, I_xi)
        return cond(predicate, deposit_gaussian, identity, pstate)

    return _deposit_gaussian


def build_bias_grad_evaluator(method: ParallelBiasMetadynamics):
    """
    Returns a function that given the deposited Gaussians parameters, computes the
    gradient of the biasing potential with respect to the CVs.
    """
    grid = method.grid
    T = method.T
    kB = method.kB
    beta = 1 / (kB * T)

    if grid is None:
        periods = get_periods(method.cvs)
        evaluate_bias_grad = jit(lambda pstate: grad(parallelbias)(*pstate[:4], beta, periods))
    else:

        def zero_force(_):
            return np.zeros(grid.shape.size)

        def get_force(pstate):
            return pstate.grid_gradient[pstate.grid_idx]

        def evaluate_bias_grad(pstate):
            ob = np.any(np.array(pstate.grid_idx) == grid.shape)  # out of bounds
            return cond(ob, zero_force, get_force, pstate)

    return evaluate_bias_grad


# Helper function to evaluate parallel bias potential
def parallelbias(xi, heights, centers, sigmas, beta, periods):
    """
    Evaluate parallel bias potential according to Eq. 8 in
    [J. Chem. Theory Comput. 11, 5062–5067 (2015)](https://doi.org/10.1021/acs.jctc.5b00846)
    """
    bias_each_cv = get_bias_each_cv(xi, heights, centers, sigmas, periods)
    exp_sum_gaussian = np.exp(-beta * bias_each_cv)

    return -(1 / beta) * np.log(np.sum(exp_sum_gaussian))


# Helper function to evaluate parallel bias potential along each CV
def get_bias_each_cv(xi, heights, centers, sigmas, periods):
    """
    Evaluate parallel bias potential along each CV.
    """
    delta_xi_each_cv = wrap(xi - centers, periods)
    gaussian_each_cv = heights * np.exp(-((delta_xi_each_cv / sigmas) ** 2) / 2)
    bias_each_cv = np.sum(gaussian_each_cv, axis=0)

    return bias_each_cv


@dispatch
def analyze(result: Result[ParallelBiasMetadynamics]):
    """
    Helper for calculating the free energy from the final state of a `Parallel Bias Metadynamics` run.

    Parameters
    ----------

    result: Result[ParallelBiasMetadynamics]:
        Result bundle containing method, final parallel bias metadynamics state, and callback.

    Returns
    -------

    dict:
        A dictionary with the following keys:

        heights: JaxArray
            Height of the Gaussian bias potential along each CV during the simulation.

        parallelbias_metapotential: Callable
            Maps a user-provided array of CV values to the corresponding deposited bias
            potential. The free energy along each user-provided CV
            range is similar to well-tempered metadynamics i.e., the free energy is equal to
            `(T + deltaT) / deltaT * parallelbias_metapotential(cv)`, where `T` is the simulation
            temperature and `deltaT` is the user-defined parameter in
            parallel bias metadynamics.
    """
    method = result.method
    states = result.states

    P = get_periods(method.cvs)

    if len(states) == 1:
        heights = states[0].heights
        centers = states[0].centers
        sigmas = states[0].sigmas

        pbmetad_potential_cv = jit(vmap(lambda x: get_bias_each_cv(x, heights, centers, sigmas, P)))
        pbmetad_potential = jit(vmap(lambda x, beta: parallelbias(x, heights, centers, sigmas, beta, P), in_axes=(0, None)))
        
        return dict(centers=centers, heights=heights, pbmetad_potential_cv=pbmetad_potential_cv, pbmetad_potential=pbmetad_potential)

    # For multiple-replicas runs we return a list heights and functions
    # (one for each replica)

    def build_pbmetapotential_cv(heights, centers, sigmas):
        return jit(vmap(lambda x: get_bias_each_cv(x, heights, centers, sigmas, P)))
    
    def build_pbmetapotential(heights, centers, sigmas):
        return jit(vmap(lambda x, beta: parallelbias(x, heights, centers, sigmas, beta, P), in_axes=(0, None)))

    heights = []
    centers = []
    pbmetad_potentials_cv = []
    pbmetad_potentials = []

    for s in states:
        centers.append(s.centers)
        heights.append(s.heights)
        pbmetad_potentials_cv.append(build_pbmetapotential_cv(s.heights, s.centers, s.sigmas))
        pbmetad_potentials.append(build_pbmetapotential(s.heights, s.centers, s.sigmas))

    return dict(centers=centers, heights=heights, pbmetad_potential_cv=pbmetad_potentials_cv, pbmetad_potential=pbmetad_potentials)
