from typing import Union, Callable
from time import time

import numpy as np
import biorbd_casadi as biorbd

from .optimal_control_program import OptimalControlProgram
from .solution import Solution
from ..dynamics.configure_problem import Dynamics, DynamicsList
from ..limits.constraints import ConstraintFcn
from ..limits.objective_functions import ObjectiveFcn
from ..limits.path_conditions import InitialGuess, Bounds
from ..misc.enums import Solver, InterpolationType


class RecedingHorizonOptimization(OptimalControlProgram):
    """
    The main class to define an MHE. This class prepares the full program and gives all
    the needed interface to modify and solve the program

    Methods
    -------
    solve(self, solver: Solver, show_online_optim: bool, solver_options: dict) -> Solution
        Call the solver to actually solve the ocp
    """

    def __init__(
        self,
        biorbd_model: Union[str, biorbd.Model, list, tuple],
        dynamics: Union[Dynamics, DynamicsList],
        window_len: Union[int, list, tuple],
        window_duration: Union[int, float, list, tuple],
        use_sx=True,
        **kwargs,
    ):
        """
        Parameters
        ----------
        window_size: Union[int, list[int]]
            The number of shooting point of the moving window
        """

        if isinstance(biorbd_model, (list, tuple)) and len(biorbd_model) > 1:
            raise ValueError("Receding horizon optimization must be defined using only one biorbd_model")

        super(RecedingHorizonOptimization, self).__init__(
            biorbd_model=biorbd_model,
            dynamics=dynamics,
            n_shooting=window_len,
            phase_time=window_duration,
            use_sx=use_sx,
            **kwargs,
        )

    def solve(
        self,
        update_function: Callable,
        solver: Solver = Solver.ACADOS,
        solver_options: dict = None,
        solver_options_first_iter: dict = None,
        export_options: dict = None,
        **extra_options,
    ) -> Solution:
        """
        Solve MHE program. The program runs until 'update_function' returns False. This function can be used to
        modify the objective set, for instance. The warm_start_function can be provided by the user. Otherwise, the
        initial guess is the solution where the first frame is dropped and the last frame is duplicated. Moreover,
        the bounds at first frame is set to the new first frame of the initial guess

        Parameters
        ----------
        update_function: Callable
            A function with the signature: update_function(mhe, current_time_index, previous_solution), where the
            mhe is the current program, current_time_index starts at 0 and increments after each solve and
            previous_solution is None the first call and then is the Solution structure for the last solving of the MHE.
            The function 'update_function' is called before each solve. If it returns true, the next frame is solve.
            Otherwise, it finishes the MHE and the solution is returned. The `update_function` callback can also
            be used to modify the program (usually the targets of some objective functions) and initial condition and
            bounds.
        solver: Solver
            The Solver to use (default being ACADOS)
        solver_options: dict
            The options to pass to the solver.
        solver_options_first_iter: dict
            A special set of options to pass to the solver for the first frame only,
            and then replaced by solver_options if present.
        export_options: dict
            Any options related to the saving of the data at each iteration
        extra_options: Any
            Any options to pass to the regular OCP

        Returns
        -------
        The solution of the MHE
        """

        if len(self.nlp) != 1:
            raise NotImplementedError("MHE is only available for 1 phase program")

        t = 0
        sol = None
        states = []
        controls = []

        if solver_options_first_iter is None and solver_options is not None:
            solver_options_first_iter = solver_options
            solver_options = None
        solver_option_current = solver_options_first_iter

        if solver == Solver.IPOPT:
            if solver_options is None:
                solver_options = solver_options_first_iter if solver_options_first_iter else {}
            if "bound_push" not in solver_options:
                solver_options["bound_push"] = 1e-10
            if "bound_frac" not in solver_options:
                solver_options["bound_frac"] = 1e-10

        if export_options is None:
            export_options = {"frame_to_export": 0}
        else:
            if "frame_to_export" not in export_options:
                export_options["frame_to_export"] = 0

        if isinstance(export_options["frame_to_export"], int):
            export_options["frame_to_export"] = slice(
                export_options["frame_to_export"], export_options["frame_to_export"] + 1
            )

        total_time = 0
        real_time = 0

        while update_function(self, t, sol):
            sol = super(RecedingHorizonOptimization, self).solve(
                solver=solver, solver_options=solver_option_current, **extra_options
            )
            solver_option_current = solver_options if t == 0 else None

            total_time += sol.real_time_to_optimize
            if t == 0:
                real_time = time()  # Skip the compile time (so skip the first call to solve)

            # Solve and save the current window
            _states, _controls = self.export_data(sol, export_options)
            states.append(_states)
            controls.append(_controls)

            # Update the initial frame bounds and initial guess
            self.advance_window(sol)

            if "show_online_optim" in extra_options and extra_options["show_online_optim"]:
                # Reuse the previous graphs
                del extra_options["show_online_optim"]

            t += 1
        real_time = time() - real_time

        # Prepare the modified ocp that fits the solution dimension
        sol = self._initialize_solution(t, states, controls)
        sol.solver_time_to_optimize = total_time
        sol.real_time_to_optimize = real_time
        return sol

    def _initialize_solution(self, t: int, states: list, controls: list):
        _states = InitialGuess(np.concatenate(states, axis=1), interpolation=InterpolationType.EACH_FRAME)
        _controls = InitialGuess(np.concatenate(controls, axis=1), interpolation=InterpolationType.EACH_FRAME)

        solution_ocp = OptimalControlProgram(
            biorbd_model=self.original_values["biorbd_model"][0],
            dynamics=self.original_values["dynamics"][0],
            n_shooting=t - 1,
            phase_time=t * self.nlp[0].dt,
            skip_continuity=True,
        )
        return Solution(solution_ocp, [_states, _controls])

    def advance_window(self, sol: Solution, steps: int = 0):
        if self.nlp[0].x_bounds.type != InterpolationType.CONSTANT_WITH_FIRST_AND_LAST_DIFFERENT:
            if self.nlp[0].x_bounds.type == InterpolationType.CONSTANT:
                x_min = np.repeat(self.nlp[0].x_bounds.min[:, 0:1], 3, axis=1)
                x_max = np.repeat(self.nlp[0].x_bounds.max[:, 0:1], 3, axis=1)
                self.nlp[0].x_bounds = Bounds(x_min, x_max)
            else:
                raise NotImplementedError(
                    "The MHE is not implemented yet for x_bounds not being "
                    "CONSTANT or CONSTANT_WITH_FIRST_AND_LAST_DIFFERENT"
                )
            self.nlp[0].x_bounds.check_and_adjust_dimensions(self.nlp[0].states.shape, 3)
        self.nlp[0].x_bounds[:, 0] = sol.states["all"][:, 1]
        if self.solver_type != Solver.ACADOS:
            self.update_bounds(self.nlp[0].x_bounds)

        if self.nlp[0].x_init.type != InterpolationType.EACH_FRAME:
            self.nlp[0].x_init = InitialGuess(
                np.ndarray(sol.states["all"].shape), interpolation=InterpolationType.EACH_FRAME
            )
            self.nlp[0].x_init.check_and_adjust_dimensions(self.nlp[0].states.shape, self.nlp[0].ns)
        self.nlp[0].x_init.init[:, :] = np.concatenate(
            (sol.states["all"][:, 1:], sol.states["all"][:, -1][:, np.newaxis]), axis=1
        )

        if self.nlp[0].u_init.type != InterpolationType.EACH_FRAME:
            self.nlp[0].u_init = InitialGuess(
                np.ndarray(sol.controls["all"][:, :-1].shape), interpolation=InterpolationType.EACH_FRAME
            )
            self.nlp[0].u_init.check_and_adjust_dimensions(self.nlp[0].controls.shape, self.nlp[0].ns - 1)
        self.nlp[0].u_init.init[:, :] = np.concatenate(
            (sol.controls["all"][:, 1:-1], sol.controls["all"][:, -2][:, np.newaxis]), axis=1
        )

        if self.solver_type != Solver.ACADOS:
            self.update_initial_guess(self.nlp[0].x_init, self.nlp[0].u_init)

    @staticmethod
    def export_data(sol, export_options) -> tuple:
        f = export_options["frame_to_export"]
        return sol.states["all"][:, f], sol.controls["all"][:, f]

    def _define_time(self, phase_time: Union[int, float, list, tuple], objective_functions, constraints):
        """
        Declare the phase_time vector in v. If objective_functions or constraints defined a time optimization,
        a sanity check is perform and the values of initial guess and bounds for these particular phases

        Parameters
        ----------
        phase_time: Union[int, float, list, tuple]
            The time of all the phases
        objective_functions: ObjectiveList
            All the objective functions. It is used to scan if any time optimization was defined
        constraints: ConstraintList
            All the constraint functions. It is used to scan if any free time was defined
        """

        def check_for_time_optimization(penalty_functions):
            """
            Make sure one does not try to optimize time

            Parameters
            ----------
            penalty_functions: Union[ObjectiveList, ConstraintList]
                The list to parse to ensure no double free times are declared

            """

            for i, penalty_functions_phase in enumerate(penalty_functions):
                for pen_fun in penalty_functions_phase:
                    if not pen_fun:
                        continue
                    if (
                        pen_fun.type == ObjectiveFcn.Mayer.MINIMIZE_TIME
                        or pen_fun.type == ObjectiveFcn.Lagrange.MINIMIZE_TIME
                        or pen_fun.type == ConstraintFcn.TIME_CONSTRAINT
                    ):
                        raise ValueError("Time cannot be optimized in Receding Horizon Optimization")

        check_for_time_optimization(objective_functions)
        check_for_time_optimization(constraints)

        super(RecedingHorizonOptimization, self)._define_time(phase_time, objective_functions, constraints)


class CyclicRecedingHorizonOptimization(RecedingHorizonOptimization):
    def solve(
        self,
        update_function: Callable,
        solver: Solver = Solver.ACADOS,
        solver_options: dict = None,
        solver_options_first_iter: dict = None,
        **extra_options,
    ) -> Solution:
        self._set_cyclic_bound()
        export_options = {"frame_to_export": slice(0, -1)}
        if solver == Solver.IPOPT:
            self.update_bounds(self.nlp[0].x_bounds)
        return super(CyclicRecedingHorizonOptimization, self).solve(
            update_function,
            solver,
            solver_options,
            solver_options_first_iter,
            export_options=export_options,
            **extra_options,
        )

    def _initialize_solution(self, t: int, states: list, controls: list):
        _states = InitialGuess(np.concatenate(states, axis=1), interpolation=InterpolationType.EACH_FRAME)
        _controls = InitialGuess(np.concatenate(controls, axis=1), interpolation=InterpolationType.EACH_FRAME)

        solution_ocp = OptimalControlProgram(
            biorbd_model=self.original_values["biorbd_model"][0],
            dynamics=self.original_values["dynamics"][0],
            n_shooting=t * self.nlp[0].ns - 1,
            phase_time=t * self.nlp[0].ns * self.nlp[0].dt,
            skip_continuity=True,
        )
        return Solution(solution_ocp, [_states, _controls])

    def _set_cyclic_bound(self):
        if self.nlp[0].x_bounds.type != InterpolationType.CONSTANT_WITH_FIRST_AND_LAST_DIFFERENT:
            raise ValueError(
                "Cyclic bounds for x_bounds should be of "
                "type InterpolationType.CONSTANT_WITH_FIRST_AND_LAST_DIFFERENT"
            )
        if self.nlp[0].u_bounds.type != InterpolationType.CONSTANT_WITH_FIRST_AND_LAST_DIFFERENT:
            raise ValueError(
                "Cyclic bounds for u_bounds should be of "
                "type InterpolationType.CONSTANT_WITH_FIRST_AND_LAST_DIFFERENT"
            )

        range_of_motion = self.nlp[0].x_bounds.max[:, 1] - self.nlp[0].x_bounds.min[:, 1]
        self.nlp[0].x_bounds.min[:, 2] = self.nlp[0].x_bounds.min[:, 0] - range_of_motion * 0.01
        self.nlp[0].x_bounds.max[:, 2] = self.nlp[0].x_bounds.max[:, 0] + range_of_motion * 0.01

    @staticmethod
    def _append_current_solution(sol: Solution, states: list, controls: list):
        states.append(sol.states["all"][:, :-1])
        controls.append(sol.controls["all"][:, :-1])

    def advance_window(self, sol: Solution, steps: int = 0):
        # Update the initial frame bounds
        self.nlp[0].x_bounds[:, 0] = sol.states["all"][:, -1]
        self._set_cyclic_bound()
        if self.solver_type == Solver.IPOPT:
            self.update_bounds(self.nlp[0].x_bounds)

        if self.nlp[0].x_init.type != InterpolationType.EACH_FRAME:
            self.nlp[0].x_init = InitialGuess(
                np.ndarray(sol.states["all"].shape), interpolation=InterpolationType.EACH_FRAME
            )
            self.nlp[0].x_init.check_and_adjust_dimensions(self.nlp[0].states.shape, self.nlp[0].ns)
        if self.nlp[0].u_init.type != InterpolationType.EACH_FRAME:
            self.nlp[0].u_init = InitialGuess(
                np.ndarray((sol.controls["all"].shape[0], self.nlp[0].ns)), interpolation=InterpolationType.EACH_FRAME
            )
            self.nlp[0].u_init.check_and_adjust_dimensions(self.nlp[0].controls.shape, self.nlp[0].ns - 1)
        self.nlp[0].x_init.init[:, :] = sol.states["all"]
        self.nlp[0].u_init.init[:, :] = sol.controls["all"][:, :-1]
        if self.solver_type == Solver.IPOPT:
            self.update_initial_guess(self.nlp[0].x_init, self.nlp[0].u_init)

        if self.solver_type == Solver.IPOPT:
            self.solver.set_lagrange_multiplier(sol)


class NonlinearModelPredictiveControl(RecedingHorizonOptimization):
    """
    NMPC version of receding horizon optimization
    """

    def __init__(
        self,
        biorbd_model: Union[str, biorbd.Model, list, tuple],
        dynamics: Union[Dynamics, DynamicsList],
        window_len: Union[int, list, tuple],
        window_duration: Union[int, float, list, tuple],
        use_sx=True,
        **kwargs,
    ):
        super(NonlinearModelPredictiveControl, self).__init__(
            biorbd_model, dynamics, window_len, window_duration, use_sx, **kwargs
        )


class CyclicNonlinearModelPredictiveControl(CyclicRecedingHorizonOptimization):
    """
    NMPC version of cyclic receding horizon optimization
    """

    pass


class MovingHorizonEstimator(RecedingHorizonOptimization):
    """
    MHE version of receding horizon optimization
    """

    def __init__(
        self,
        biorbd_model: Union[str, biorbd.Model, list, tuple],
        dynamics: Union[Dynamics, DynamicsList],
        window_len: Union[int, list, tuple],
        window_duration: Union[int, float, list, tuple],
        use_sx=True,
        **kwargs,
    ):
        super(MovingHorizonEstimator, self).__init__(
            biorbd_model, dynamics, window_len, window_duration, use_sx, **kwargs
        )


class CyclicMovingHorizonEstimator(CyclicRecedingHorizonOptimization):
    """
    MHE version of cyclic receding horizon optimization
    """

    pass