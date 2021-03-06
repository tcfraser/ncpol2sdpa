# -*- coding: utf-8 -*-
"""
The module converts a noncommutative optimization problem provided in SymPy
format to an SDPA semidefinite programming problem.

Created on Sun May 26 15:06:17 2013

@author: Peter Wittek
"""
from __future__ import division, print_function
import sys
from math import floor, sqrt
from sympy import S, Expr, expand
import numpy as np
import time
try:
    import multiprocessing
    from functools import partial
except ImportError:
    pass
try:
    from scipy.linalg import qr
    from scipy.sparse import lil_matrix, hstack
except ImportError:
    from .sparse_utils import lil_matrix
from .nc_utils import apply_substitutions, build_permutation_matrix, \
                      check_simple_substitution, convert_relational, \
                      find_variable_set, flatten, \
                      get_all_monomials, is_number_type, \
                      is_pure_substitution_rule, iscomplex, ncdegree, \
                      pick_monomials_up_to_degree, save_monomial_index, \
                      separate_scalar_factor, simplify_polynomial, unique
from .solver_common import find_solution_ranks, get_sos_decomposition, \
                           get_xmat_value, solve_sdp, extract_dual_value
from .mosek_utils import convert_to_mosek
from .picos_utils import convert_to_picos
from .sdpa_utils import write_to_sdpa, write_to_human_readable
from .chordal_extension import find_variable_cliques


class Relaxation(object):

    def __init__(self):
        """Constructor for the class.
        """
        self.n_vars = 0
        self.F_struct = None
        self.block_struct = []
        self.obj_facvar = 0
        self.constant_term = 0
        # Variables related to the solution
        self.primal = None
        self.dual = None
        self.x_mat = None
        self.y_mat = None
        self.solution_time = None
        self.status = "unsolved"
        self.constraints_hash = None

    def solve(self, solver=None, solverparameters=None):
        """Call a solver on the SDP relaxation. Upon successful solution, it
        returns the primal and dual objective values along with the solution
        matrices. It also sets these values in the `sdpRelaxation` object,
        along with some status information.

        :param sdpRelaxation: The SDP relaxation to be solved.
        :type sdpRelaxation: :class:`ncpol2sdpa.SdpRelaxation`.
        :param solver: The solver to be called, either `None`, "sdpa", "mosek",
                       or "cvxopt". The default is `None`, which triggers
                       autodetect.
        :type solver: str.
        :param solverparameters: Parameters to be passed to the solver. Actual
                                 options depend on the solver:

                                 SDPA:

                                   - `"executable"`:
                                     Specify the executable for SDPA. E.g.,
                                     `"executable":"/usr/local/bin/sdpa"`, or
                                     `"executable":"sdpa_gmp"`
                                   - `"paramsfile"`: Specify the parameter file

                                 Mosek:
                                 Refer to the Mosek documentation. All
                                 arguments are passed on.

                                 Cvxopt:
                                 Refer to the PICOS documentation. All
                                 arguments are passed on.
        :type solverparameters: dict of str.
        """
        if self.F_struct is None:
            raise Exception("Relaxation is not generated yet. Call "
                            "'SdpRelaxation.get_relaxation' first")
        solve_sdp(self, solver, solverparameters)


class SdpRelaxation(Relaxation):

    """Class for obtaining sparse SDP relaxation.

    :param variables: Commutative or noncommutative, Hermitian or nonhermiatian
                      variables, possibly a list of list of variables if the
                      hierarchy is not NPA.
    :type variables: list of :class:`sympy.physics.quantum.operator.Operator`
                     or
                     :class:`sympy.physics.quantum.operator.HermitianOperator`
                     or a list of list.
    :param parameters: Optional symbolic variables for which moments are not
                       generated.
    :type parameters: list of :class:`sympy.physics.quantum.operator.Operator`
                     or
                     :class:`sympy.physics.quantum.operator.HermitianOperator`
                     or a list of list.
    :param verbose: Optional parameter for level of verbosity:

                       * 0: quiet
                       * 1: verbose
                       * 2: debug level
    :type verbose: int.
    :param normalized: Optional parameter for changing the normalization of
                       states over which the optimization happens. Turn it off
                       if further processing is done on the SDP matrix before
                       solving it.
    :type normalized: bool.
    :param parallel: Optional parameter for allowing parallel computations.
    :type parallel: bool.

    Attributes:
      - `monomial_sets`: The monomial sets that generate the moment matrix blocks.

      - `monomial_index`: Dictionary that maps monomials to SDP variables.

      - `constraints`: The complete set of constraints after preprocesssing.

      - `primal`: The primal optimal value.

      - `dual`: The dual optimal value.

      - `x_mat`: The primal solution matrix.

      - `y_mat`: The dual solution matrix.

      - `solution_time`: The amount of time taken to solve the relaxation.

      - `status`: The solution status of the relaxation.

    """
    def __init__(self, variables, parameters=None, verbose=0, normalized=True,
                 parallel=False):
        """Constructor for the class.
        """
        super(SdpRelaxation, self).__init__()
        self.substitutions = {}

        #: Dictionary that maps monomials to SDP variables.
        self.monomial_index = {}

        self.var_offsets = [0]
        self.variables = []
        self.verbose = verbose
        self.localizing_monomial_sets = None
        self.normalized = normalized
        self.constraint_starting_block = 0
        self.level = 0
        self.monomial_sets = []
        self.pure_substitution_rules = True
        self.constraints = []
        self.moment_substitutions = {}
        self.complex_matrix = False
        n_noncommutative_hermitian = 0
        n_noncommutative_nonhermitian = 0
        n_commutative_hermitian = 0
        n_commutative_nonhermitian = 0
        if isinstance(variables, list):
            if len(variables) > 0 and (isinstance(variables[0], list) or
                                       isinstance(variables[0], tuple)):
                self.variables = [unique(vs) for vs in variables]
            else:
                self.variables = unique(variables)
        else:
            self.variables = [variables]
        for vs in self.variables:
            if not isinstance(vs, list) and not isinstance(vs, tuple):
                vs = [vs]
            for v in vs:
                if v.is_commutative and (v.is_hermitian is None or
                                         v.is_hermitian):
                    n_commutative_hermitian += 1
                elif v.is_commutative:
                    n_commutative_nonhermitian += 1
                elif not v.is_commutative and (v.is_hermitian is None or
                                               v.is_hermitian):
                    n_noncommutative_hermitian += 1
                else:
                    n_noncommutative_nonhermitian += 1
        self.parameters = parameters
        info = ""
        if n_commutative_hermitian > 0:
            info += str(n_commutative_hermitian) + " commuting"
        if n_commutative_nonhermitian > 0:
            if len(info) > 0:
                info += ", "
            info += str(n_commutative_nonhermitian) + \
                " commuting nonhermitian"
        if n_noncommutative_hermitian > 0:
            if len(info) > 0:
                info += ", "
            info += str(n_noncommutative_hermitian) + \
                " noncommuting Hermitian"
        if n_noncommutative_nonhermitian > 0:
            if len(info) > 0:
                info += ", "
            info += str(n_commutative_nonhermitian) + \
                " noncommuting nonhermitian"
        if len(info) > 0:
            info += " variables"
        else:
            info += "0 variables"
        info = "The problem has " + info
        if parameters is not None:
            info += ", and " + str(len(flatten(parameters))) + \
                "symbolic parameters"
        if self.verbose > 0:
            print(info)
        self._parallel = False
        if parallel:
            try:
                multiprocessing
                self._parallel = parallel
            except:
                print("Warning: multiprocessing cannot be imported!")

    ########################################################################
    # ROUTINES RELATED TO GENERATING THE MOMENT MATRICES                   #
    ########################################################################

    def _process_monomial(self, monomial, n_vars):
        """Process a single monomial when building the moment matrix.
        """
        monomial, coeff = separate_scalar_factor(monomial)
        k = 0
        # Are we substituting this for a constant?
        try:
            coeff2 = self.moment_substitutions[monomial]
            coeff *= coeff2
        except KeyError:
            # Have we seen this monomial before?
            try:
                # If yes, then we improve sparsity by reusing the
                # previous variable to denote this entry in the matrix
                k = self.monomial_index[monomial]
            except KeyError:
                # Otherwise we define a new entry in the associated
                # array recording the monomials, and add an entry in
                # the moment matrix
                k = n_vars + 1
                self.monomial_index[monomial] = k
        return k, coeff

    def _push_monomial(self, monomial, n_vars, row_offset, rowA, columnA, N,
                       rowB, columnB, lenB, prevent_substitutions=False):
        if not prevent_substitutions:
            monomial = apply_substitutions(monomial, self.substitutions,
                                           self.pure_substitution_rules)
        if is_number_type(monomial):
            if rowA == 0 and columnA == 0 and rowB == 0 and columnB == 0 and \
              monomial == 1.0:
                if not self.normalized:
                    n_vars += 1
                    self.F_struct[row_offset + rowA * N*lenB +
                                  rowB * N + columnA * lenB + columnB,
                                  n_vars] = 1
                else:
                    self.F_struct[row_offset + rowA * N*lenB +
                                  rowB * N + columnA * lenB + columnB,
                                  0] = float(self.normalized)
            else:
                self.F_struct[row_offset + rowA * N*lenB +
                              rowB * N + columnA * lenB + columnB,
                              0] = monomial
        elif monomial.is_Add:
            for element in monomial.as_ordered_terms():
                n_vars = self._push_monomial(element, n_vars, row_offset,
                                             rowA, columnA, N,
                                             rowB, columnB, lenB, True)
        elif monomial != 0:
            k, coeff = self._process_monomial(monomial, n_vars)
            # We push the entry to the moment matrix
            self.F_struct[row_offset + rowA * N*lenB +
                          rowB * N +
                          columnA * lenB + columnB, k] = coeff
            if k > n_vars:
                n_vars = k
        return n_vars

    def _generate_moment_matrix(self, n_vars, block_index, processed_entries,
                                monomialsA, monomialsB, ppt=False):
        """Generate the moment matrix of monomials.

        Arguments:
        n_vars -- current number of variables
        block_index -- current block index in the SDP matrix
        monomials -- |W_d| set of words of length up to the relaxation level
        """
        row_offset = 0
        if block_index > 0:
            for block_size in self.block_struct[0:block_index]:
                row_offset += block_size ** 2
        N = len(monomialsA)*len(monomialsB)
        if not self._parallel:
            # We process the M_d(u,w) entries in the moment matrix
            for rowA in range(len(monomialsA)):
                for columnA in range(rowA, len(monomialsA)):
                    for rowB in range(len(monomialsB)):
                        start_columnB = 0
                        if rowA == columnA:
                            start_columnB = rowB
                        for columnB in range(start_columnB, len(monomialsB)):
                            processed_entries += 1
                            if (not ppt) or (columnB >= rowB):
                                monomial = monomialsA[rowA].adjoint() * \
                                           monomialsA[columnA] * \
                                           monomialsB[rowB].adjoint() * \
                                           monomialsB[columnB]
                            else:
                                monomial = monomialsA[rowA].adjoint() * \
                                           monomialsA[columnA] * \
                                           monomialsB[columnB].adjoint() * \
                                           monomialsB[rowB]
                            # Apply the substitutions if any
                            n_vars = self._push_monomial(monomial, n_vars,
                                                         row_offset, rowA,
                                                         columnA, N, rowB,
                                                         columnB, len(monomialsB))
                if self.verbose > 0:
                    percentage = \
                        "{0:.0f}%".format(float(processed_entries-1)/self.n_vars *
                                          100)
                    sys.stdout.write("\r\x1b[KCurrent number of SDP variables: %d"
                                     " (done: %s)" % (n_vars, percentage))
                    sys.stdout.flush()
        else:
            time0 = time.time()
            pool = multiprocessing.Pool()
            func = partial(assemble_monomial_and_do_substitutions, monomialsA=monomialsA, monomialsB=monomialsB, ppt=ppt, substitutions=self.substitutions, pure_substitution_rules=self.pure_substitution_rules)
            chunksize = max(int(sqrt(len(monomialsA)*len(monomialsB)*len(monomialsA)/2)/multiprocessing.cpu_count()),1)#this is just a guess and can be optimized
            pooliter = pool.imap(func, (
                (rowA, columnA, rowB)
                for rowA in range(len(monomialsA))
                for rowB in range(len(monomialsB))
                for columnA in range(rowA, len(monomialsA))
            ), chunksize)

            for rowA, columnA, rowB, columnB, monomial in pooliter:
                processed_entries += 1
                n_vars = self._push_monomial(monomial, n_vars,
                                             row_offset, rowA,
                                             columnA, N, rowB,
                                             columnB, len(monomialsB),
                                             prevent_substitutions=True)
                if self.verbose > 0:
                    percentage = \
                        "{0:.0f}%".format(float(processed_entries-1)/self.n_vars *
                                          100)
                    sys.stdout.write("\r\x1b[KCurrent number of SDP variables: %d"
                                     " (done: %s, working in %s processes for %s seconds with a chunksize of %s)" % (n_vars, percentage, multiprocessing.cpu_count(), int(time.time()-time0), chunksize))
                    sys.stdout.flush()

            pool.close()
            pool.join()


        if self.verbose > 0:
            sys.stdout.write("\r")
        return n_vars, block_index + 1, processed_entries

    def _generate_all_moment_matrix_blocks(self, n_vars, block_index):
        processed_entries = 0
        for monomials in self.monomial_sets:
            if len(monomials) > 0 and isinstance(monomials[0], list):
                if len(monomials[0]) != len(monomials[1]):
                    raise Exception("Cannot generate square block from "
                                    "unequal monomial lists!")
                n_vars, block_index, processed_entries = \
                    self._generate_moment_matrix(
                        n_vars,
                        block_index,
                        processed_entries,
                        monomials[0], monomials[1])
            else:
                n_vars, block_index, processed_entries = \
                    self._generate_moment_matrix(
                        n_vars,
                        block_index,
                        processed_entries,
                        monomials, [S.One])
            self.var_offsets.append(n_vars)
        return n_vars, block_index

    ########################################################################
    # ROUTINES RELATED TO GENERATING THE LOCALIZING MATRICES AND PROCESSING#
    # CONSTRAINTS                                                          #
    ########################################################################

    def _get_index_of_monomial(self, element, enablesubstitution=True,
                               daggered=False):
        """Returns the index of a monomial.
        """
        processed_element, coeff1 = separate_scalar_factor(element)
        if enablesubstitution:
            processed_element = \
                apply_substitutions(processed_element, self.substitutions,
                                    self.pure_substitution_rules)
        # Given the monomial, we need its mapping L_y(w) to push it into
        # a corresponding constraint matrix
        if processed_element.is_Number:
            return [(0, coeff1)]
        elif processed_element.is_Add:
            monomials = \
                processed_element.as_coeff_mul()[1][0].as_coeff_add()[1]
        else:
            monomials = [processed_element]
        result = []
        for monomial in monomials:
            monomial, coeff2 = separate_scalar_factor(monomial)
            coeff = coeff1*coeff2
            if monomial.is_Number:
                result.append((0, coeff))
                continue
            k = -1
            if monomial != 0:
                if monomial.as_coeff_Mul()[0] < 0:
                    monomial = -monomial
                    coeff = -1.0 * coeff
            try:
                coeff0 = self.moment_substitutions[monomial]
                result.append((0, coeff0*coeff))
            except KeyError:
                try:
                    k = self.monomial_index[monomial]
                    result.append((k, coeff))
                except KeyError:
                    if not daggered:
                        dag_result = self._get_index_of_monomial(monomial.adjoint(),
                                                                 daggered=True)
                        result += [(k, coeff0*coeff) for k, coeff0 in dag_result]
                    else:
                        raise RuntimeError("The requested monomial " +
                                           str(monomial) + " could not be found.")
        return result

    def __push_facvar_sparse(self, polynomial, block_index, row_offset, i, j):
        """Calculate the sparse vector representation of a polynomial
        and pushes it to the F structure.
        """
        width = self.block_struct[block_index - 1]
        # Preprocess the polynomial for uniform handling later
        # DO NOT EXPAND THE POLYNOMIAL HERE!!!!!!!!!!!!!!!!!!!
        # The simplify_polynomial bypasses the problem.
        # Simplifying here will trigger a bug in SymPy related to
        # the powers of daggered variables.
        # polynomial = polynomial.expand()
        if is_number_type(polynomial) or polynomial.is_Mul:
            elements = [polynomial]
        else:
            elements = polynomial.as_coeff_mul()[1][0].as_coeff_add()[1]
        # Identify its constituent monomials
        for element in elements:
            results = self._get_index_of_monomial(element)
            # k identifies the mapped value of a word (monomial) w
            for (k, coeff) in results:
                if k > -1 and coeff != 0:
                    self.F_struct[row_offset + i * width + j, k] += coeff

    def _get_facvar(self, polynomial):
        """Return dense vector representation of a polynomial. This function is
        nearly identical to __push_facvar_sparse, but instead of pushing
        sparse entries to the constraint matrices, it returns a dense
        vector.
        """
        facvar = [0] * (self.n_vars + 1)
        # Preprocess the polynomial for uniform handling later
        if is_number_type(polynomial):
            facvar[0] = polynomial
            return facvar
        polynomial = polynomial.expand()
        if polynomial.is_Mul:
            elements = [polynomial]
        else:
            elements = polynomial.as_coeff_mul()[1][0].as_coeff_add()[1]
        for element in elements:
            results = self._get_index_of_monomial(element)
            for (k, coeff) in results:
                facvar[k] += coeff
        return facvar

    def __process_inequalities(self, block_index):
        """Generate localizing matrices

        Arguments:
        inequalities -- list of inequality constraints
        monomials    -- localizing monomials
        block_index -- the current block index in constraint matrices of the
                       SDP relaxation
        """
        initial_block_index = block_index
        row_offsets = [0]
        for block, block_size in enumerate(self.block_struct):
            row_offsets.append(row_offsets[block] + block_size ** 2)





        if not self._parallel:
            for k, ineq in enumerate(self.constraints):
                block_index += 1
                if isinstance(ineq, str):
                    self.__parse_expression(ineq, row_offsets[block_index-1])
                    continue
                if ineq.is_Relational:
                    ineq = convert_relational(ineq)
                monomials = self.localizing_monomial_sets[block_index -
                                                          initial_block_index-1]
                # Process M_y(gy)(u,w) entries
                for row in range(len(monomials)):
                    for column in range(row, len(monomials)):
                        # Calculate the moments of polynomial entries
                        polynomial = \
                            simplify_polynomial(
                                monomials[row].adjoint() * expand(ineq) *
                                monomials[column], self.substitutions)
                        self.__push_facvar_sparse(polynomial, block_index,
                                                  row_offsets[block_index-1],
                                                  row, column)
                if self.verbose > 0:
                    sys.stdout.write("\r\x1b[KProcessing %d/%d constraints..." %
                                     (k+1, len(self.constraints)))
                    sys.stdout.flush()
        else:
            pool = multiprocessing.Pool()
            for k, ineq in enumerate(self.constraints):
                block_index += 1
                if isinstance(ineq, str):
                    self.__parse_expression(ineq, row_offsets[block_index-1])
                    continue
                if ineq.is_Relational:
                    ineq = convert_relational(ineq)
                monomials = self.localizing_monomial_sets[block_index -
                                                          initial_block_index-1]
                func = partial(moment_of_entry, monomials=monomials, ineq=ineq, substitutions=self.substitutions)
                chunksize = max(int(sqrt(len(monomials)*len(monomials)/2)/multiprocessing.cpu_count()),1)#this is just a guess and can be optimized
                pooliter = pool.imap(func,
                                     ([row,column]
                                      for row in range(len(monomials))
                                      for column in range(row, len(monomials))
                                     ), chunksize)

                for row, column, polynomial in pooliter:
                    self.__push_facvar_sparse(polynomial, block_index,
                                              row_offsets[block_index-1],
                                              row, column)
                if self.verbose > 0:
                    sys.stdout.write("\r\x1b[KProcessing %d/%d constraints..." %
                                     (k+1, len(self.constraints)))
                    sys.stdout.flush()

            pool.close()
            pool.join()

        if self.verbose > 0:
            sys.stdout.write("\n")
        return block_index

    def __process_equalities(self, equalities, momentequalities):
        """Generate localizing matrices

        Arguments:
        equalities -- list of equality constraints
        equalities -- list of moment equality constraints
        """
        monomial_sets = []
        n_rows = 0
        if equalities is not None:
            for equality in equalities:
                # Find the order of the localizing matrix
                if equality.is_Relational:
                    equality = convert_relational(equality)
                eq_order = ncdegree(equality)
                if eq_order > 2 * self.level:
                    raise Exception("An equality constraint has degree %d. "
                                    "Choose a higher level of relaxation."
                                    % eq_order)
                localization_order = int(floor((2 * self.level - eq_order)/2))
                index = find_variable_set(self.variables, equality)
                localizing_monomials = \
                    pick_monomials_up_to_degree(self.monomial_sets[index],
                                                localization_order)
                if len(localizing_monomials) == 0:
                    localizing_monomials = [S.One]
                localizing_monomials = unique(localizing_monomials)
                monomial_sets.append(localizing_monomials)
                n_rows += len(localizing_monomials) * \
                    (len(localizing_monomials) + 1) // 2
        if momentequalities is not None:
            for _ in momentequalities:
                monomial_sets.append([S.One])
                n_rows += 1
        A = np.zeros((n_rows, self.n_vars + 1), dtype=self.F_struct.dtype)
        n_rows = 0
        for i, equality in enumerate(flatten([equalities, momentequalities])):
            # Process M_y(gy)(u,w) entries
            for row in range(len(monomial_sets[i])):
                for column in range(row, len(monomial_sets[i])):
                    # Calculate the moments of polynomial entries
                    polynomial = \
                        simplify_polynomial(monomial_sets[i][row].adjoint() *
                                            equality*monomial_sets[i][column],
                                            self.substitutions)
                    A[n_rows] = self._get_facvar(polynomial)
                    # This is something really weird: we want the constant
                    # terms in equalities to be positive. Otherwise funny
                    # things happen in the QR decomposition and the basis
                    # transformation.
                    if A[n_rows, 0] < 0:
                        A[n_rows] = -A[n_rows]
                    n_rows += 1
        return A

    def __remove_equalities(self, equalities, momentequalities):
        """Attempt to remove equalities by solving the linear equations.
        """
        A = self.__process_equalities(equalities, momentequalities)
        if A.shape[0] == 0:
            return
        c = np.array(self.obj_facvar)
        Q, R, P = qr(np.transpose(A[:, 1:]), pivoting=True)
        E = build_permutation_matrix(P)
        n = np.max(np.nonzero(np.sum(np.abs(R), axis=1) > 0)) + 1
        x = np.dot(Q[:, :n], np.linalg.solve(np.transpose(R[:n, :]),
                                             E.T.dot(-A[:, 0])))
        x = np.append(1, x)
        H = lil_matrix(Q[:, n:])  # New basis
        # Transforming the objective function
        self.obj_facvar = H.T.dot(c)
        # Transforming the moment matrix and localizing matrices
        self.F_struct = self.F_struct[:,:self.n_vars+1]
        new_constant_column = lil_matrix(self.F_struct.dot(x))
        self.F_struct = hstack([new_constant_column.T,
                                self.F_struct[:, 1:].dot(H)])
        self.F_struct = self.F_struct.tolil()
        self.n_vars = self.F_struct.shape[1] - 1

    def __duplicate_momentmatrix(self, original_n_vars, n_vars, block_index):
        self.var_offsets.append(n_vars)
        row_offset = 0
        for block_size in self.block_struct[0:block_index]:
            row_offset += block_size ** 2
        width = self.block_struct[0]
        for row in range(width**2):
            self.F_struct[row_offset + row,
                          n_vars+1:n_vars + original_n_vars+2] =\
              self.F_struct[row, :original_n_vars+1]
        return n_vars + original_n_vars + 1, block_index + 1

    def __add_new_momentmatrix(self, n_vars, block_index):
        self.var_offsets.append(n_vars)
        row_offset = 0
        for block_size in self.block_struct[0:block_index]:
            row_offset += block_size ** 2
        width = self.block_struct[0]
        for i in range(width):
            for j in range(i, width):
                n_vars += 1
                self.F_struct[row_offset + i * width + j, n_vars] = 1
        return n_vars, block_index + 1

    def __impose_ppt(self, block_index):
        row_offset = 0
        for block_size in self.block_struct[0:block_index-1]:
            row_offset += block_size ** 2
        lenA = len(self.monomial_sets[0])
        lenB = len(self.monomial_sets[1])
        N = lenA*lenB
        for rowA in range(lenA):
            for columnA in range(rowA, lenA):
                for rowB in range(lenB):
                    start_columnB = 0
                    if rowA == columnA:
                        start_columnB = rowB
                    for columnB in range(start_columnB, rowB):
                        original_row = self.F_struct[row_offset + rowA*N*lenB +
                                                     rowB * N +
                                                     columnA * lenB + columnB]
                        self.F_struct[row_offset + rowA*N*lenB +
                                      rowB * N +
                                      columnA * lenB + columnB] = \
                            self.F_struct[row_offset + rowA*N*lenB +
                                          columnB * N + columnA * lenB + rowB]
                        self.F_struct[row_offset + rowA*N*lenB + columnB * N +
                                      columnA * lenB + rowB] = original_row

    def __add_extra_momentmatrices(self, extramomentmatrices, n_vars,
                                   block_index):
        original_n_vars = n_vars
        if extramomentmatrices is not None:
            for parameters in extramomentmatrices:
                copy = False
                ppt = False
                for parameter in parameters:
                    if parameter == "copy":
                        copy = True
                    if parameter == "ppt":
                        ppt = True
                if copy:
                    n_vars, block_index = \
                      self.__duplicate_momentmatrix(original_n_vars, n_vars,
                                                    block_index)
                else:
                    n_vars, block_index = \
                      self.__add_new_momentmatrix(n_vars, block_index)
                if ppt:
                    self.__impose_ppt(block_index)
        return n_vars, block_index

    def __parse_expression(self, expr, row_offset):
        if expr.find("]") > -1:
            sub_exprs = expr.split(']')
            for sub_expr in sub_exprs:
                startindex = 0
                if sub_expr.startswith('-') or sub_expr.startswith('+'):
                    startindex = 1
                ind = sub_expr.find('[')
                if ind > -1:
                    idx = sub_expr[ind+1:].split(",")
                    i, j = int(idx[0]), int(idx[1])
                    mm_ind = int(sub_expr[startindex:ind])
                    if sub_expr.find('*') > -1:
                        value = float(sub_expr[:sub_expr.find('*')])
                    elif sub_expr.startswith('-'):
                        value = -1.0
                    else:
                        value = 1.0
                    base_row_offset = sum([bs**2 for bs in
                                           self.block_struct[:mm_ind]])
                    width = self.block_struct[mm_ind]
                    self.F_struct[row_offset] += \
                        value*self.F_struct[base_row_offset + i*width + j]
                else:
                    value = float(sub_expr)
                    self.F_struct[row_offset, 0] += value

    ########################################################################
    # ROUTINES RELATED TO INITIALIZING DATA STRUCTURES                     #
    ########################################################################

    def _calculate_block_structure(self, inequalities, equalities, bounds,
                                   momentinequalities, momentequalities,
                                   extramomentmatrix, removeequalities,
                                   block_struct=None):
        """Calculates the block_struct array for the output file.
        """
        if block_struct is None:
            if self.verbose > 0:
                print("Calculating block structure...")
            self.block_struct = []
            if self.parameters is not None:
                self.block_struct.append(-len(self.parameters))
            for monomials in self.monomial_sets:
                if len(monomials) > 0 and isinstance(monomials[0], list):
                    self.block_struct.append(len(monomials[0]))
                else:
                    self.block_struct.append(len(monomials))
            if extramomentmatrix is not None:
                for _ in extramomentmatrix:
                    for monomials in self.monomial_sets:
                        if len(monomials) > 0 and \
                                isinstance(monomials[0], list):
                            self.block_struct.append(len(monomials[0]))
                        else:
                            self.block_struct.append(len(monomials))
        else:
            self.block_struct = block_struct
        degree_warning = False
        if inequalities is not None:
            n_inequalities = len(inequalities)
        else:
            n_inequalities = 0
        if removeequalities:
            constraints = enumerate(flatten([inequalities]))
        else:
            constraints = enumerate(flatten([inequalities, equalities]))
        monomial_sets = []
        for k, constraint in constraints:
            # Find the order of the localizing matrix
            if isinstance(constraint, str):
                ineq_order = 2 * self.level
            else:
                if constraint.is_Relational:
                    constraint = convert_relational(constraint)
                ineq_order = ncdegree(constraint)
                if iscomplex(constraint):
                    self.complex_matrix = True
            if ineq_order > 2 * self.level:
                degree_warning = True
            localization_order = int(floor((2 * self.level - ineq_order) / 2))
            if self.level == -1:
                localization_order = 0
            if self.localizing_monomial_sets is not None and \
                    self.localizing_monomial_sets[k] is not None:
                localizing_monomials = self.localizing_monomial_sets[k]
            else:
                index = find_variable_set(self.variables, constraint)
                localizing_monomials = \
                    pick_monomials_up_to_degree(self.monomial_sets[index],
                                                localization_order)
            if len(localizing_monomials) == 0:
                localizing_monomials = [S.One]
            localizing_monomials = unique(localizing_monomials)
            monomial_sets.append(localizing_monomials)
            self.block_struct.append(len(localizing_monomials))
            if k >= n_inequalities:
                monomial_sets.append(localizing_monomials)
                self.block_struct.append(len(localizing_monomials))

        if degree_warning and self.verbose > 0:
            print("A constraint has degree %d. Either choose a higher level "
                  "relaxation or ensure that a mixed-order relaxation has the"
                  " necessary monomials" % (ineq_order))

        if bounds is not None:
            for _ in bounds:
                monomial_sets.append([S.One])
                self.block_struct.append(1)
        if momentinequalities is not None:
            for _ in momentinequalities:
                monomial_sets.append([S.One])
                self.block_struct.append(1)
        if momentequalities is not None:
            for moment_eq in momentequalities:
                substitution = check_simple_substitution(moment_eq)
                if substitution == (0, 0):
                    if not removeequalities:
                        monomial_sets += [[S.One], [S.One]]
                        self.block_struct += [1, 1]
                else:
                    self.moment_substitutions[substitution[0]] = \
                        substitution[1]
        self.localizing_monomial_sets = monomial_sets

    def __generate_monomial_sets(self, extramonomials):
        if self.level == -1:
            if extramonomials is None or extramonomials == []:
                raise Exception("Cannot build relaxation at level -1 without \
                                monomials specified.")
            if isinstance(extramonomials[0], list):
                self.monomial_sets = extramonomials
            else:
                self.monomial_sets.append(extramonomials)
            return
        if isinstance(self.variables[0], list):
            k = 0
            for variables in self.variables:
                extramonomials_ = None
                if extramonomials is not None:
                    extramonomials_ = extramonomials[k]
                self.monomial_sets.append(
                    get_all_monomials(variables, extramonomials_,
                                      self.substitutions, self.level))
                k += 1
        else:
            if extramonomials is not None and len(extramonomials) > 0 and \
                  isinstance(extramonomials[0], list):
                self.monomial_sets.append(
                    get_all_monomials(self.variables, extramonomials[0],
                                      self.substitutions, self.level))
                self.monomial_sets += extramonomials[1:]
            else:
                self.monomial_sets.append(
                    get_all_monomials(self.variables, extramonomials,
                                      self.substitutions, self.level))

    def _estimate_n_vars(self):
        self.n_vars = 0
        if self.parameters is not None:
            self.n_vars = len(self.parameters)
        for monomials in self.monomial_sets:
            if len(monomials) > 0 and isinstance(monomials[0], list):
                n_monomials = len(monomials[0])
            else:
                n_monomials = len(monomials)

            # The minus one compensates for the constant term in the
            # top left corner of the moment matrix
            self.n_vars += int(n_monomials * (n_monomials + 1) / 2)
            if self.normalized:
                self.n_vars -= 1

    def __add_parameters(self):
        new_n_vars, block_index = 0, 0
        if self.parameters is not None:
            block_index = 1
            for var in self.parameters:
                new_n_vars += 1
                self.monomial_index[var] = new_n_vars
                self.F_struct[new_n_vars - 1, new_n_vars] = 1
        return new_n_vars, block_index

    def __wipe_F_struct_from_constraints(self):
        row_offset = 0
        for block in range(self.constraint_starting_block):
            row_offset += self.block_struct[block]**2
        for row in range(row_offset, len(self.F_struct.rows)):
            self.F_struct.rows[row] = []
            self.F_struct.data[row] = []

    ########################################################################
    # PUBLIC ROUTINES EXPOSED TO THE USER                                  #
    ########################################################################

    def process_constraints(self, inequalities=None, equalities=None,
                            bounds=None, momentinequalities=None,
                            momentequalities=None, block_index=0,
                            removeequalities=False):
        """Process the constraints and generate localizing matrices. Useful
        only if the moment matrix already exists. Call it if you want to
        replace your constraints. The number of the respective types of
        constraints and the maximum degree of each constraint must remain the
        same.

        :param inequalities: Optional parameter to list inequality constraints.
        :type inequalities: list of :class:`sympy.core.exp.Expr`.
        :param equalities: Optional parameter to list equality constraints.
        :type equalities: list of :class:`sympy.core.exp.Expr`.
        :param momentinequalities: Optional parameter of inequalities defined
                                   on moments.
        :type momentinequalities: list of :class:`sympy.core.exp.Expr`.
        :param momentequalities: Optional parameter of equalities defined
                                 on moments.
        :type momentequalities: list of :class:`sympy.core.exp.Expr`.
        :param removeequalities: Optional parameter to attempt removing the
                                 equalities by solving the linear equations.

        :param removeequalities: Optional parameter to attempt removing the
                                 equalities by solving the linear equations.
        :type removeequalities: bool.
        """
        if block_index == 0 or block_index == self.constraint_starting_block:
            if self.constraints_hash == hash(frozenset((str(inequalities),str(equalities),str(bounds),str(momentinequalities),str(momentequalities),str(removeequalities)))):
                return
            self.constraints_hash = hash(frozenset((str(inequalities),str(equalities),str(bounds),str(momentinequalities),str(momentequalities),str(removeequalities))))

        self.status = "unsolved"
        if block_index == 0:
            block_index = self.constraint_starting_block
            self.__wipe_F_struct_from_constraints()
        self.constraints = flatten([inequalities])
        if not (removeequalities or equalities is None):
            # Equalities are converted to pairs of inequalities
            for equality in equalities:
                if equality.is_Relational:
                    equality = convert_relational(equality)
                self.constraints.append(equality)
                self.constraints.append(-equality)
        if bounds is not None:
            for bound in bounds:
                self.constraints.append(bound)
        if momentinequalities is not None:
            for mineq in momentinequalities:
                self.constraints.append(mineq)
        reduced_moment_equalities = []
        if momentequalities is not None:
            for meq in momentequalities:
                substitution = check_simple_substitution(meq)
                if substitution == (0, 0):
                    if not removeequalities:
                        self.constraints.append(meq)
                        if isinstance(meq, str):
                            tmp = meq.replace("+", "p")
                            tmp = tmp.replace("-", "+")
                            tmp = tmp.replace("p", "-")
                            self.constraints.append(tmp)
                        else:
                            self.constraints.append(-meq)
                    else:
                        reduced_moment_equalities.append(meq)
        self.__process_inequalities(block_index)
        if reduced_moment_equalities == []:
            reduced_moment_equalities = None
        if removeequalities:
            self.__remove_equalities(equalities, reduced_moment_equalities)

    def set_objective(self, objective, extraobjexpr=None):
        """Set or change the objective function of the polynomial optimization
        problem.

        :param objective: Describes the objective function.
        :type objective: :class:`sympy.core.expr.Expr`
        :param extraobjexpr: Optional parameter of a string expression of a
                             linear combination of moment matrix elements to be
                             included in the objective function
        :type extraobjexpr: str.
        """
        if objective is not None:
            facvar = \
                self._get_facvar(simplify_polynomial(objective,
                                                     self.substitutions))
            self.obj_facvar = facvar[1:]
            self.constant_term = facvar[0]
            if self.verbose > 0 and facvar[0] != 0:
                print("Warning: The objective function has a non-zero %s "
                      "constant term. It is not included in the SDP objective."
                      % facvar[0])
        else:
            self.obj_facvar = self._get_facvar(0)[1:]
        if extraobjexpr is not None:
            for sub_expr in extraobjexpr.split(']'):
                startindex = 0
                if sub_expr.startswith('-') or sub_expr.startswith('+'):
                    startindex = 1
                ind = sub_expr.find('[')
                if ind > -1:
                    idx = sub_expr[ind+1:].split(",")
                    i, j = int(idx[0]), int(idx[1])
                    mm_ind = int(sub_expr[startindex:ind])
                    if sub_expr.find('*') > -1:
                        value = float(sub_expr[:sub_expr.find('*')])
                    elif sub_expr.startswith('-'):
                        value = -1.0
                    else:
                        value = 1.0
                    base_row_offset = sum([bs**2 for bs in
                                           self.block_struct[:mm_ind]])
                    width = self.block_struct[mm_ind]
                    for column in self.F_struct[base_row_offset + i*width + j].rows[0]:
                        self.obj_facvar[column-1] = \
                            value*self.F_struct[base_row_offset + i*width + j,
                                                column]

    def __getitem__(self, index):
        """Obtained the value for a polynomial in a solved relaxation.

        :param index: The polynomial.
        :type index: `sympy.core.exp.Expr`

        :returns: The value of the polynomial extracted from the solved SDP.
        :rtype: float
        """
        if not isinstance(index, Expr):
            raise Exception("Not a monomial or polynomial!")
        elif self.status == "unsolved":
            raise Exception("SDP relaxation is not solved yet!")
        else:
            return get_xmat_value(index, self)

    def get_sos_decomposition(self, threshold=0.0):
        """Given a solution of the dual problem, it returns the SOS
        decomposition.

        :param sdpRelaxation: The SDP relaxation to be solved.
        :type sdpRelaxation: :class:`ncpol2sdpa.SdpRelaxation`.
        :param threshold: Optional parameter for specifying the threshold value
                          below which the eigenvalues and entries of the
                          eigenvectors are disregarded.
        :type threshold: float.
        :returns: The SOS decomposition of [sigma_0, sigma_1, ..., sigma_m]
        :rtype: list of :class:`sympy.core.exp.Expr`.
        """
        return get_sos_decomposition(self, threshold=threshold)

    def extract_dual_value(self, monomial, blocks=None):
        """Given a solution of the dual problem and a monomial, it returns the
        inner product of the corresponding coefficient matrix and the dual
        solution. It can be restricted to certain blocks.

        :param monomial: The monomial for which the value is requested.
        :type monomial: :class:`sympy.core.exp.Expr`.
        :param monomial: The monomial for which the value is requested.
        :type monomial: :class:`sympy.core.exp.Expr`.
        :param blocks: Optional parameter to specify the blocks to be included.
        :type blocks: list of `int`.
        :returns: The value of the monomial in the solved relaxation.
        :rtype: float.
        """
        return extract_dual_value(self, monomial, blocks)

    def find_solution_ranks(self, xmat=None, baselevel=0):
        """Helper function to detect rank loop in the solution matrix.

        :param sdpRelaxation: The SDP relaxation.
        :type sdpRelaxation: :class:`ncpol2sdpa.SdpRelaxation`.
        :param x_mat: Optional parameter providing the primal solution of the
                      moment matrix. If not provided, the solution is extracted
                      from the sdpRelaxation object.
        :type x_mat: :class:`numpy.array`.
        :param base_level: Optional parameter for specifying the lower level
                           relaxation for which the rank loop should be tested
                           against.
        :type base_level: int.
        :returns: list of int -- the ranks of the solution matrix with in the
                  order of increasing degree.
        """
        return find_solution_ranks(self, xmat=xmat, baselevel=baselevel)

    def write_to_file(self, filename, filetype=None):
        """Write the relaxation to a file.

        :param filename: The name of the file to write to. The type can be
                         autodetected from the extension: .dat-s for SDPA,
                         .task for mosek or .csv for human readable format.
        :type filename: str.
        :param filetype: Optional parameter to define the filetype. It can be
                         "sdpa" for SDPA , "mosek" for Mosek, or "csv" for
                         human readable format.
        :type filetype: str.
        """
        if filetype == "sdpa" and not filename.endswith(".dat-s"):
            raise Exception("SDPA files must have .dat-s extension!")
        if filetype == "mosek" and not filename.endswith(".task"):
            raise Exception("Mosek files must have .task extension!")
        elif filetype is None and filename.endswith(".dat-s"):
            filetype = "sdpa"
        elif filetype is None and filename.endswith(".csv"):
            filetype = "csv"
        elif filetype is None and filename.endswith(".task"):
            filetype = "mosek"
        elif filetype is None:
            raise Exception("Cannot detect filetype from extension!")

        if filetype == "sdpa":
            write_to_sdpa(self, filename)
        elif filetype == "mosek":
            task = convert_to_mosek(self)
            task.writedata(filename)
        elif filetype == "csv":
            write_to_human_readable(self, filename)
        else:
            raise Exception("Unknown filetype")

    def save_monomial_index(self, filename):
        """Write the monomial index to a file.

        :param filename: The name of the file to write to.
        :type filename: str.
        """
        save_monomial_index(filename, self.monomial_index)

    def convert_to_picos(self, duplicate_moment_matrix=False):
        """Convert the SDP relaxation to a PICOS problem such that the exported
        .dat-s file is extremely sparse, there is not penalty imposed in terms
        of SDP variables or number of constraints. This conversion can be used
        for imposing extra constraints on the moment matrix, such as partial
        transpose.

        :param duplicate_moment_matrix: Optional parameter to add an
                                        unconstrained moment matrix to the
                                        problem with the same structure as the
                                        moment matrix with the PSD constraint.
        :type duplicate_moment_matrix: bool.

        :returns: :class:`picos.Problem`.
        """
        return \
            convert_to_picos(self,
                             duplicate_moment_matrix=duplicate_moment_matrix)

    def convert_to_mosek(self):
        """Convert an SDP relaxation to a MOSEK task.

        :returns: :class:`mosek.Task`.
        """
        return convert_to_mosek(self)

    def get_relaxation(self, level, objective=None, inequalities=None,
                       equalities=None, substitutions=None, bounds=None,
                       momentinequalities=None, momentequalities=None,
                       removeequalities=False, extramonomials=None,
                       extramomentmatrices=None, extraobjexpr=None,
                       localizing_monomials=None, chordal_extension=False):
        """Get the SDP relaxation of a noncommutative polynomial optimization
        problem.

        :param level: The level of the relaxation. The value -1 will skip
                      automatic monomial generation and use only the monomials
                      supplied by the option `extramonomials`.
        :type level: int.
        :param obj: Optional parameter to describe the objective function.
        :type obj: :class:`sympy.core.exp.Expr`.
        :param inequalities: Optional parameter to list inequality constraints.
        :type inequalities: list of :class:`sympy.core.exp.Expr`.
        :param equalities: Optional parameter to list equality constraints.
        :type equalities: list of :class:`sympy.core.exp.Expr`.
        :param substitutions: Optional parameter containing monomials that can
                              be replaced (e.g., idempotent variables).
        :type substitutions: dict of :class:`sympy.core.exp.Expr`.
        :param momentinequalities: Optional parameter of inequalities defined
                                   on moments.
        :type momentinequalities: list of :class:`sympy.core.exp.Expr`.
        :param momentequalities: Optional parameter of equalities defined
                                 on moments.
        :type momentequalities: list of :class:`sympy.core.exp.Expr`.
        :param removeequalities: Optional parameter to attempt removing the
                                 equalities by solving the linear equations.
        :type removeequalities: bool.
        :param extramonomials: Optional paramter of monomials to be included,
                               on top of the requested level of relaxation.
        :type extramonomials: list of :class:`sympy.core.exp.Expr`.
        :param extramomentmatrices: Optional paramter of duplicating or adding
                               moment matrices.  A new moment matrix can be
                               unconstrained (""), a copy  of the first one
                               ("copy"), and satisfying a partial positivity
                               constraint ("ppt"). Each new moment matrix is
                               requested as a list of string of these options.
                               For instance, adding a single new moment matrix
                               as a copy of the first would be
                               ``extramomentmatrices=[["copy"]]``.
        :type extramomentmatrices: list of list of str.
        :param extraobjexpr: Optional parameter of a string expression of a
                             linear combination of moment matrix elements to be
                             included in the objective function.
        :type extraobjexpr: str.
        :param localizing_monomials: Optional parameter to specify sets of
                                     localizing monomials for each constraint.
                                     The internal order of constraints is
                                     inequalities first, followed by the
                                     equalities. If the parameter is specified,
                                     but for a certain constraint the automatic
                                     localization is requested, leave None in
                                     its place in this parameter.
        :type localizing_monomials: list of list of `sympy.core.exp.Expr`.
        :param chordal_extension: Optional parameter to request a sparse
                                  chordal extension.
        :type chordal_extension: bool.

        """
        if self.level < -1:
            raise Exception("Invalid level of relaxation")
        self.level = level
        if substitutions is None:
            self.substitutions = {}
        else:
            self.substitutions = substitutions
            for lhs, rhs in substitutions.items():
                if not is_pure_substitution_rule(lhs, rhs):
                    self.pure_substitution_rules = False
                if iscomplex(lhs) or iscomplex(rhs):
                    self.complex_matrix = True
        if chordal_extension:
            self.variables = find_variable_cliques(self.variables, objective,
                                                   inequalities, equalities)
        self.__generate_monomial_sets(extramonomials)
        self.localizing_monomial_sets = localizing_monomials

        if bounds is not None:
            print("The optional parameter `bounds' is deprecated. Please use"
                  " the parameter `momentinequalities' or `momentequalities'")
        # Figure out basic structure of the SDP
        self._calculate_block_structure(inequalities, equalities, bounds,
                                        momentinequalities, momentequalities,
                                        extramomentmatrices,
                                        removeequalities)
        self._estimate_n_vars()
        if extramomentmatrices is not None:
            for parameters in extramomentmatrices:
                copy = False
                for parameter in parameters:
                    if parameter == "copy":
                        copy = True
                if copy:
                    self.n_vars += self.n_vars + 1
                else:
                    self.n_vars += (self.block_struct[0]**2)/2
        if self.complex_matrix:
            dtype = np.complex128
        else:
            dtype = np.float64
        self.F_struct = lil_matrix((sum([bs**2 for bs in self.block_struct]),
                                    self.n_vars + 1), dtype=dtype)

        if self.verbose > 0:
            print(('Estimated number of SDP variables: %d' % self.n_vars))
            print('Generating moment matrix...')
        # Generate moment matrices
        new_n_vars, block_index = self.__add_parameters()
        new_n_vars, block_index = \
            self._generate_all_moment_matrix_blocks(new_n_vars, block_index)
        if extramomentmatrices is not None:
            new_n_vars, block_index = \
                self.__add_extra_momentmatrices(extramomentmatrices,
                                                new_n_vars, block_index)
        # The initial estimate for the size of F_struct was overly generous.
        self.n_vars = new_n_vars
        # We don't correct the size of F_struct, because that would trigger
        # memory copies, and extra columns in lil_matrix are free anyway.
        # self.F_struct = self.F_struct[:, 0:self.n_vars + 1]

        if self.verbose > 0:
            print(('Reduced number of SDP variables: %d' % self.n_vars))
        # Objective function
        self.set_objective(objective, extraobjexpr)
        # Process constraints
        self.constraint_starting_block = block_index
        self.process_constraints(inequalities, equalities, bounds,
                                 momentinequalities, momentequalities,
                                 block_index, removeequalities)


def moment_of_entry(pos, monomials, ineq, substitutions):
    row = pos[0]
    column = pos[1]

    return row, column, simplify_polynomial(
        monomials[row].adjoint() * ineq *
        monomials[column], substitutions)


def assemble_monomial_and_do_substitutions(arg, monomialsA, monomialsB, ppt, substitutions, pure_substitution_rules):
    rowA = arg[0]
    columnA = arg[1]
    rowB = arg[2]

    start_columnB = 0
    if rowA == columnA:
        start_columnB = rowB
    for columnB in range(start_columnB, len(monomialsB)):
        if (not ppt) or (columnB >= rowB):
            monomial = monomialsA[rowA].adjoint() * \
                       monomialsA[columnA] * \
                       monomialsB[rowB].adjoint() * \
                       monomialsB[columnB]
        else:
            monomial = monomialsA[rowA].adjoint() * \
                       monomialsA[columnA] * \
                       monomialsB[columnB].adjoint() * \
                       monomialsB[rowB]
            # Apply the substitutions if any
        monomial = apply_substitutions(monomial, substitutions,
                                       pure_substitution_rules)
    return rowA, columnA, rowB, columnB, monomial
