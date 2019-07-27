# -*- coding: utf-8 -*-
# ***************************************************************************
# *                                                                         *
# *   Copyright (c) 2017 - Qingfeng Xia <qingfeng.xia iesensor.com>         *
# *                                                                         *
# *   This program is free software; you can redistribute it and/or modify  *
# *   it under the terms of the GNU Lesser General Public License (LGPL)    *
# *   as published by the Free Software Foundation; either version 2 of     *
# *   the License, or (at your option) any later version.                   *
# *   for detail see the LICENCE text file.                                 *
# *                                                                         *
# *   This program is distributed in the hope that it will be useful,       *
# *   but WITHOUT ANY WARRANTY; without even the implied warranty of        *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         *
# *   GNU Library General Public License for more details.                  *
# *                                                                         *
# *   You should have received a copy of the GNU Library General Public     *
# *   License along with this program; if not, write to the Free Software   *
# *   Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  *
# *   USA                                                                   *
# *                                                                         *
# ***************************************************************************


from __future__ import print_function, division, absolute_import

"""
Feature: provide basic shared methods like translate_value(), build mesh and function_space

'transient_settings'
+ default to fixed time step, specifying `time_step`, can specify a numpy.array of time_points
+ temporal differentiation
    - Crank-Nicolson (2nd , unconditionally stable for diffusion problem) for ScalarTransportSolver
    - NS function, backward Euler for the time being.
    
"""

# make python2 and python3 compatible for unicode type, basestring is not existed in Python3
import sys
if sys.version_info[0]>=3:
   #str = str
   unicode = str
   bytes = bytes
   basestring = (str,bytes)
else:
   str = str
   #unicode = unicode
   bytes = str
   basestring = basestring


import numbers
import copy
import logging
import numpy as np
import os.path

# import math may cause error
from dolfin import *

class SolverError(Exception):
    pass

default_report_settings  = {"logging_level": logging.DEBUG,  "logging_file": None,
                            "plotting_freq": 10, 'plotting_interactive': True, 'plotting_file': None,
                            'saving_freq': 10, 'result_filename': None}

# directly mapping to solver.parameters of Fenics
default_solver_parameters = {"relative_tolerance": 1e-5,
                             "maximum_iterations": 500,
                             "monitor_convergence": True,  # print to console
                             }
default_case_settings = {'solver_name': None,
                'case_name': 'test', 'case_folder': "./",  'case_file': None,  # if used by GUI tool, may be removed later
                'mesh':  None, 'fe_degree': 1, 'fe_family': "CG",
                'function_space': None, 'periodic_boundary': None, 
                'boundary_conditions': None, # OrderedDict
                'body_source': None,  # dict for different subdomains {"sub_name": {'subdomain_id': 1, 'value': 2}}
                'surface_source': None,  # apply to all boundary,  {'value': 100, 'direction': Constant(1,0,0)} without direction mean normal
                'initial_values': {},  # dict with key as scalar or vector name
                'material':{},  # can be a list of material dict for different subdomains
                'solver_settings': {
                    'transient_settings': {'transient': False, 'starting_time': 0, 'time_step': 0.01, 'ending_time': 0.03},
                    'reference_values': {},
                    'solver_parameters': default_solver_parameters,
                    },
                "report_settings": default_report_settings
                }

class SolverBase():
    """ shared base class for all fenics solver with utilty functions
    solve(), plot(), get_variables(), 
    generate_form() and update_boundary_conditions() must be implemented by derived class
    """
    def __init__(self, case_input):
        if isinstance(case_input, (dict)):
            self.settings = case_input
            #self.print()
            self.load_settings(case_input)
        else:
            raise SolverError('case setup data must be a python dict')
        # Fenics 2018.1:Rename mpi_comm_world() to MPI.comm_world.
        try:
            mpi_comm_world_size = MPI.size(mpi_comm_world())
            #suppress output from other process
            if MPI.rank(mpi_comm_world()) != 0:
                #set_log_level(LogLevel.CRITICAL)
                set_log_active(False)  # complete turn off logging.
        except:
            mpi_comm_world_size = MPI.size(MPI.comm_world)
            #suppress output from other process
            if MPI.rank(MPI.comm_world) != 0:
                #set_log_level(LogLevel.CRITICAL)
                set_log_active(False)  # complete turn off logging.
        if mpi_comm_world_size >1:
            self.parallel = True
        else:
            self.parallel = False

    def print(self):
        import pprint
        pp = pprint.PrettyPrinter(indent=4)
        pp.pprint(self.settings)

    def load_settings(self, s):
        if 'periodic_boundary' not in s:  # check: settings file can not store None element?
            s['periodic_boundary'] = None
        ## mesh and boundary
        self.boundary_conditions = s['boundary_conditions']  # used by generate_boundary_facets()
        if ('mesh' in s) and s['mesh']:
            if isinstance(s['mesh'], (str, unicode)):
                self.read_mesh(s['mesh'])  # it also read boundary
            elif isinstance(s['mesh'], (Mesh,)):
                self.mesh = s['mesh']
                self.generate_boundary_facets()
            else:
                raise SolverError('Error: mesh must be file path or Mesh object: {}')
            if not 'fe_family' in s:
                s['fe_family'] = 'CG'
            if  not 'fe_degree' in s:
                s['fe_degree'] = 1
            self.generate_function_space(s['periodic_boundary'])
        elif ('mesh' not in s or s['mesh']==None) and ('function_space' in s and s['function_space']):
            self.function_space = s['function_space']
            s['fe_degree']  = self.function_space._ufl_element.degree()
            if not 'fe_family' in s:
                s['fe_family'] = 'CG'  # auto detect? 
            self.mesh = self.function_space.mesh()
            self.generate_boundary_facets()
            self.is_mixed_function_space = False
        else:
            raise SolverError('mesh or function space must specified to construct solver object')
        self.dimension = self.mesh.geometry().dim()
        self.topo_dimension = self.mesh.topology().dim()  # for  != 0, mesh could be topo =2, geom = 3

        if not hasattr(self, 'subdomains'):  # useful to set nulti-region material and body_source
            self.subdomains = MeshFunction("size_t", self.mesh, self.mesh.topology().dim())
        ##
        if 'body_source' in s and s['body_source']:
            self.body_source = s['body_source']
        else:
            self.body_source = None

        ## initial and reference values
        if 'initial_values' in s:
            self.initial_values = s['initial_values']
        else:
            self.initial_values = {}
        self.reference_values = s['solver_settings']['reference_values']
        
        ## material
        self.material = s['material']
        
        ## solver setting, transient settings
        self.solver_settings = s['solver_settings']
        self.transient_settings = s['solver_settings']['transient_settings']
        self.transient = self.transient_settings['transient']

        if 'report_settings' not in self.settings:
            self.settings['report_settings'] = default_report_settings
        self.report_settings = self.settings['report_settings'] 
        self.set_logger(self.settings['report_settings'])

    def set_logger(self, s):
        logger = logging.getLogger(self.__class__.__name__)
        # create console handler and set level to debug
        if ('logging_file' not in s) or (s['logging_file'] == None):
            fh = logging.StreamHandler()
        else:
            fh = logging.FileHandler(s['logging_file'])
        if 'logging_level' in s:
            fh.setLevel(s['logging_level'])
        else:
            fh.setLevel(logging.DEBUG)
        # create formatter
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)

        # add console stdout or log file to logger
        logger.addHandler(fh)
        self.logger = logger  # usage: self.logger.debug(msg)

    def _read_hdf5_mesh(self, filename):
        # path is identical to FenicsSolver.utility 
        mesh = Mesh()
        hdf = HDF5File(mesh.mpi_comm(), filename, "r")
        hdf.read(mesh, "/mesh", False)
        self.mesh = mesh

        self.subdomains = MeshFunction("size_t", mesh, mesh.topology().dim())
        if (hdf.has_dataset("/subdomains")):
            hdf.read(self.subdomains, "/subdomains")
        else:
            print('Subdomain file is not provided')

        if (hdf.has_dataset("/boundaries")):
            self.boundary_facets = MeshFunction("size_t", mesh, mesh.topology().dim()-1)
            hdf.read(self.boundary_facets, "/boundaries")
        else:
            print('Boundary facets file is not provided, marked from boundary settings')
            self.generate_boundary_facets()  # boundary marking from subdomain instance

    def _read_xml_mesh(self, filename):
        mesh = Mesh(filename)
        bmeshfile = filename[:-4] + "_facet_region.xml"
        self.mesh = mesh

        if os.path.exists(bmeshfile):
            self.boundary_facets = MeshFunction("size_t", mesh, bmeshfile)
        else:
            print('Boundary facets are not provided by xml input file, boundary will be marked from subdomain instance')
            self.generate_boundary_facets()  # boundary marking from subdomain instance

        subdomain_meshfile = filename[:-4] + "_physical_region.xml"
        if os.path.exists(subdomain_meshfile):
            self.subdomains = MeshFunction("size_t", mesh, subdomain_meshfile)
        else:
            self.subdomains = MeshFunction("size_t", mesh, mesh.topology().dim())

    def read_mesh(self, filename):
        print(filename, type(filename))
        if sys.version_info[0]<3 and isinstance(filename, (unicode,)):
            filename = filename.encode('utf-8')
        if not os.path.exists(filename):
            raise SolverError('mesh file: {} , does not exist'. format(filename))
        if filename[-5:] == ".xdmf":  # there are some new feature in 2017.2
            mesh = Mesh()
            f = XDMFFile(mpi_comm_world(), filename)
            f.read(mesh, True)
            self.generate_boundary_facets()
            self.subdomains = MeshFunction("size_t", mesh, mesh.topology().dim())
            self.mesh = mesh
        elif filename[-4:] == ".xml":
            self._read_xml_mesh(filename)
        elif filename[-3:] == ".h5" or filename[-5:] == ".hdf5":
            self._read_hdf5_mesh(filename)
        else:
            raise SolverError('mesh or function space must specified to construct solver object')

    def generate_function_space(self, periodic_boundary):
        self.is_mixed_function_space = False  # todo: how to detect it is mixed?
        if "scalar_name" in self.settings:
            if periodic_boundary:
                self.function_space = FunctionSpace(self.mesh, self.settings['fe_family'], self.settings['fe_degree'], constrained_domain=periodic_boundary)
                # the group and degree of the FE element.
            else:
                self.function_space = FunctionSpace(self.mesh, self.settings['fe_family'], self.settings['fe_degree'])
        elif "vector_name" in self.settings:
            if periodic_boundary:
                self.function_space = VectorFunctionSpace(self.mesh, self.settings['fe_family'], self.settings['fe_degree'], constrained_domain=periodic_boundary)
                # the group and degree of the FE element.
            else:
                self.function_space = VectorFunctionSpace(self.mesh, self.settings['fe_family'], self.settings['fe_degree'])
        else:
            raise SolverError('only scalar or vector solver has a base method of generate_function_space()')

    def generate_boundary_facets(self):
        boundary_facets = MeshFunction('size_t', self.mesh, self.mesh.topology().dim()-1)
        boundary_facets.set_all(0)
        ## boundary conditions applying
        for name, bc in self.boundary_conditions.items():
            bc['boundary'].mark(boundary_facets, bc['boundary_id'])
        self.boundary_facets = boundary_facets

    def get_initial_field(self):
        # must return Function, currently only support single scalar or vector
        if not self.initial_values:
            #if isinstance(self.function_space, (VectorFunctionSpace,)):
            if self.is_mixed_function_space:
                u0 = Function(self.function_space)
                u0.vector()[:] = 0.0 # default to zero
                return u0
            elif 'vector_name' in self.settings:
                v0 = (0, ) * self.dimension
            elif 'scalar_name' in self.settings:
                v0 = 0
            else:
                raise SolverError('only vector and scalar equation can run this method')
        else:
            if self.is_mixed_function_space:
                raise SolverError('only vector and scalar function can run this method')
            elif 'vector_name' in self.settings:
                v0 = self.initial_values[self.settings['vector_name']]
            elif 'scalar_name' in self.settings:
                v0 = self.initial_values[self.settings['scalar_name']]
            else:
                raise SolverError('only vector and scalar function can run this method')

        if 'vector_name' in self.settings and isinstance(v0[0], (str, numbers.Number)):
            _initial_values_expr = Expression( tuple([str(v) for v in v0]), degree = self.settings['fe_degree'])
            u0 = interpolate(_initial_values_expr, self.function_space)
        elif 'scalar_name' in self.settings and isinstance(v0, (str, numbers.Number)):
            _initial_values_expr = Expression(str(v0), degree = self.settings['fe_degree'])
            u0 = interpolate(_initial_values_expr, self.function_space)
        elif isinstance(v0, (Function,)):
            try:
                u0 = Function(v0)  # same mesh and function space
            except:
                u0 = project(v0, self.function_space)
        elif os.path.exists(v0):  # a filename containg a GenericVector
            Function(self.function_space, v0)
        else:
            raise SolverError('only number, file, another function, str expr are supported as initial values')
        return u0

    def get_material_value(self, value):
        if isinstance(value, (list, tuple, np.ndarray)) and len(value) == self.dimension:
            if len(value[0]) == self.dimension:  # anisotropic material matrix, tensor
                if isinstance(value[0][0], (numbers.Number,)):
                    return as_matrix(value)
        elif isinstance(value, dict):  # inhomogeneous, multi-region values
            return self._translate_dict_value(value)
        elif isinstance(value, (numbers.Number,)):
            return value
        # TODO: nonlinear, function/expression of temperature, or any variable
        else:  # linear homogenous material, str, Expression, numbers.Number, Constant, Callable
            return value # self.translate_value(value)

    def _translate_dict_value_to_function(self, value):
        """ body source, initial value or material for multiple subdomains 
        dict input format: {'region1': {'subdomain_id': 1, 'material': metal_material}, ...}
        for performance reason, numpy array is operated directly, or cpp expression
        """
        v0 = Function(self.function_space)
        for k, v in value.item():
            raise NotImplementedError('not yet implemented')
        return v0

    def translate_value(self, value, function_space = None):
        # for both internal and boundary values
        _degree = self.settings['fe_degree']
        if function_space:
            W = function_space
        else:
            W = self.function_space
        if isinstance(value, (tuple, list, np.ndarray)):  # json dump tuple into list
            if len(value) == self.dimension and isinstance(value[0], (numbers.Number)):
                if isinstance(value, list):
                    value = tuple(value)
                values_0 = Constant(value)
            elif len(value) == self.dimension and isinstance(value[0], (str)):
                if isinstance(value, list):
                    value = Constant(tuple(value))
                values_0 = interpolate(Expression(value, degree = _degree), W)
            elif self.transient_settings['transient'] and len(value) > self.dimension:
                values_0 = value[self.current_step]
            else:
                print(' {} is supplied, but only tuple of number and string expr of dim = len(v) are supported'.format(type(value)))
        elif isinstance(value, (numbers.Number)):
            values_0 = Constant(value)
        elif isinstance(value, (Constant, Function)):
            values_0 = value  # leave it as it is, since they can be used in equation
        elif isinstance(value, (Expression, )): 
            # FIXME can not interpolate an expression, not necessary?
            values_0 = value  # interpolate(value, W)
        elif callable(value) and self.transient_settings['transient']:  # Function is also callable
            values_0 = value(self.get_current_time())
        elif isinstance(value, (str, )):  # file or string expression
            if os.path.exists(value):
                # also possible continue from existent solution, or interpolate from diff mesh density
                values_0 = Function(W)
                File(value) >> values_0
                #project(velocity, self.vector_space)  # FIXME: diff element degree is not tested
                import fenicstools
                values_0 = fenicstools.interpolate_nonmatching_mesh(values_0 , W)
            else:  # C++ expressing string
                values_0 = interpolate(Expression(value, degree = _degree), W)
        elif value == None:
            raise TypeError('None type is supplied as value to be translated')
        else:
            print('Warning: {} is supplied, not tuple, number, Constant,file name, Expression'.format(type(value)))
            values_0 = value
        return values_0

    def get_variable_name(self):
        if 'scalar_name' in self.settings:
            return self.settings['scalar_name']
        elif 'vector_name' in self.settings:
            return self.settings['vector_name']
        else:
            return 'unknown'

    def get_boundary_variable(self, bc, variable=None):
        if not variable:
            variable = self.get_variable_name()
        #print('variable = ', variable)
        bvariable = bc
        if 'values' in bc:
            if isinstance(bc['values'], dict) and variable in bc['values']:
                bvariable = bc['values'][variable]
            if isinstance(bc['values'], list):
                for vbc in bc['values']:
                    if 'variable' in vbc and vbc['variable'] == variable:
                        bvariable = vbc
        return bvariable

    def get_boundary_value(self, bc, variable=None):
        if 'values' in bc:  # new style, boundary contains a 'values' list
            if not variable:
                variable = self.get_variable_name()
            for vbc in bc['values']:
                if vbc['variable'] == variable:
                    bvalue = vbc['value']
        else:
            bvalue = bc['value']
        return translate_value(bvalue)

    def get_body_source(self):
        if isinstance(self.body_source, (dict)):  # a dict of subdomain, perhaps easier by giving an Expression
            vdict = copy.copy(self.body_source)
            for k in vdict:
                vdict[k]['value'] = self.translate_value(self.body_source[k]['value'])
            return vdict
        else:
            if self.body_source:
                return self.translate_value(self.body_source)
            else:
                return None

    def get_time_step(self, time_iter_):
        ## fixed step, but could be supplied with an np.array/list
        try:
            dt = float(self.transient_settings['time_step'])
        except:
            ts = self.transient_settings['time_series']
            if len(ts) >= time_iter_:
                dt = ts[time_iter_] - ts[time_iter_]
            else:
                print('time step can only be a sequence or scalar')
        #self.mesh.hmin()  # Compute minimum cell diameter. courant number
        return dt

    def get_current_time(self, time_iter_=None):
        if not time_iter_:
            time_iter_ = self.current_step
        #self.current_time
        try:
            dt = float(self.transient_settings['time_step'])
            tp = self.transient_settings['starting_time'] + dt * (time_iter_ - 1)
        except:
            if len(self.transient_settings['time_series']) >= time_iter_:
                tp = self.transient_settings['time_series'][time_iter_]
            else:
                print('time point can only be a sequence of time series or derived from constant time step')
        return tp

    def init_solver(self):
        self.trial_function = TrialFunction(self.function_space)
        self.test_function = TestFunction(self.function_space)
        # Define functions for transient loop
        self.w_current = self.get_initial_field()  # init to default or user provided constant
        self.w_prev = Function(self.function_space)
        self.w_prev.assign(self.w_current)
        self.w_pp = Function(self.function_space)  # previous previous value, for dynamic and high order temporal scheme
        self.w_pp.assign(self.w_current)

    def get_acceleration(self, time_iter_):
        # FIXME:  it does not works for non-uniform time step
        assert time_iter_ >= 1  # acceleration can only be calc since the second step
        vel = Constant(1 / self.get_time_step(time_iter_)) * (self.w_current - self.w_prev)
        vel_prev = Constant(1 / self.get_time_step(time_iter_ - 1)) * (self.w_prev - self.w_pp)
        return (vel - vel_prev) / Constant(1 / self.get_time_step(time_iter_))

    def solve_current_step(self):
        # only NS equation needs current value to build form
        F, Dirichlet_bcs_up = self.generate_form(self.current_step, self.trial_function, self.test_function, self.w_current, self.w_prev)
        self.w_pp.assign(self.w_prev)
        self.w_prev.assign(self.w_current)
        self.w_current = self.solve_form(F, self.w_current, Dirichlet_bcs_up)  # solve for each time step, up_prev tis not needed
        self.result = self.w_current

    def solve_transient(self):
        # boundary and source change does not change left hand side (stiffness matrix) which should be reusable
        self.init_solver()

        ts = self.transient_settings
        # Define a parameters for a stationary loop
        self.current_time = ts['starting_time']
        self.current_step = 0
        if ts['transient']:
            t_end = ts['ending_time']
        else:
            t_end = self.current_time+ 1

        sf = self.report_settings['saving_freq']
        if sf and sf>0:
            if 'result_filename' in self.report_settings and self.report_settings['result_filename']:
                result_filename = self.report_settings['result_filename']
            else:
                result_filename = 'result_file.pvd'  # default filename

        #print(ts, self.current_time, t_end)
        # Transient loop also works for steady, by set `t_end = self.time_step`
        timer_solver_all = Timer("TimerSolveAll")  # 2017.2 Ubuntu Python2 errors
        timer_solver_all.start()
        while (self.current_time < t_end):
            if ts['transient']:
                dt = self.get_time_step(self.current_step)
            else:
                dt = 1

            ## overloaded by derived classes, maybe move out of temporal loop if boundary does not change form
            self.solve_current_step()

            print("Current step = ", self.current_step, "time = ", self.current_time, " TimerSolveAll = ", timer_solver_all.elapsed())
            pf = self.report_settings['plotting_freq']
            if pf>0 and self.current_step> 0 and (self.current_step % pf == 0):
                self.plot()
            # stop for steady case, or update time

            if sf and sf>0:
                if self.current_step > 0 and (self.current_step % sf == 0):
                    self.save(result_filename)  # 
                    print("save data to file `{}` at step: {} , at time: {}". format(result_filename, self.current_step , self.current_time))
            if not self.transient_settings['transient']:
                break
            self.current_step += 1
            self.current_time += dt
        ## end of time loop
        timer_solver_all.stop()

        return self.w_current

    def solve(self):
        self.result = self.solve_transient()
        return self.result

    def plot(self):
        try:
            ver = dolfin.dolfin_version().split('.')
        except:
            import dolfin
            ver = dolfin.__version__.split('.')
        #if self.report_settings['plotting_interactive']:
        if int(ver[0]) <= 2017 and int(ver[1])<2:
            self.using_matplotlib = False  # using VTK
        else:
            self.using_matplotlib = True
        if not self.is_mixed_function_space:
            plot(self.result)  # title = "Value at time: " + str(self.current_time)
        else:
            self.plot_result()

        if not self.using_matplotlib:
            interactive()  
        else:
            import matplotlib.pyplot as plt
            plt.show()

    def save(self, result_filename):
        #currently support only pvd, this format support parallel IO
        #XDMFFile is preferred for parallel IO, checkpoint
        #how to deal with DG and higher order element? velocity of NS has order 2
        if (not self.is_mixed_function_space):
            result_stream = File(result_filename)
            result_stream << (self.w_current, self.current_time)
        else:
            # write all var into one pvd is possible as multiblock dataset
            suffix = '.pvd'
            assert result_filename[-4:] == '.pvd'
            result_filename_root = result_filename[:-4]
            ret = split(self.result)
            for i, var in enumerate(ret):
                var_name = self.settings['mixed_variable'][i]
                # AttributeError: 'ListTensor' object has no attribute 'rename',  for LargeDeformationSolver
                var.rename(var_name, "label")  # why renaming does not show in vtu file?
                var_result_filename = result_filename_root + '_' + var_name + suffix
                result_stream = File(var_result_filename)
                result_stream << (var, self.current_time)

    ####################################
    def solve_linear_problem(self, F, u, Dirichlet_bcs):
        if  'point_source' in self.settings and self.settings['point_source']:
            a_T, L_T = system(F)
            A_T = assemble(a_T)
            b_T = assemble(L_T)
            #for bc in bcs: print(type(bc))
            for bc in Dirichlet_bcs:
                if isinstance(bc, DirichletBC):
                    bc.apply(A_T, b_T)  # apply Dirichlet BC and PointSource
                else:
                    bc.apply(b_T)  # apply Dirichlet BC and PointSource
            solver = LinearSolver()  # default LU solver
            self.set_solver_parameters(solver)

            solver.solve(A_T, u.vector(), b_T)
        else:
            problem = LinearVariationalProblem(lhs(F), rhs(F), u, Dirichlet_bcs)
            solver = LinearVariationalSolver(problem)
            self.set_solver_parameters(solver)

            solver.solve()
        return u

    def solve_nonlinear_problem(self, F, u_current, Dirichlet_bcs, J):
        problem = NonlinearVariationalProblem(F, u_current, Dirichlet_bcs, J)
        solver = NonlinearVariationalSolver(problem)

        #TODO: set nonlinear solver parameters from settings dict, same option as linear solver?
        #[print(p) for p in solver.parameters['newton_solver']]
        # see all default parameters: <https://github.com/FEniCS/dolfin/blob/master/dolfin/nls/NewtonSolver.cpp>

        self.set_solver_parameters(solver)

        solver.solve()
        return u_current

    def set_solver_parameters(self, solver):
        # Define a dolfin linear algobra solver parameters

        parameters["linear_algebra_backend"] = "PETSc"  #UMFPACK: out of memory, PETSc divergent
        #parameters["linear_algebra_backend"] = "Eigen"  # 'uBLAS' is not supported any longer

        parameters["mesh_partitioner"] = "SCOTCH"
        #parameters["form_compiler"]["representation"] = "quadrature"
        parameters["form_compiler"]["optimize"] = True

        if 'solver_parameters' in self.solver_settings:
            for key in self.solver_settings['solver_parameters']:
                if key in solver.parameters:
                    solver.parameters[key] = self.solver_settings['solver_parameters'][key]

    def solve_amg(self, F, u, bcs):
        A, b = assemble_system(lhs(F), rhs(F), bcs)
        # Create near null space basis (required for smoothed aggregation AMG).
        # The solution vector is passed so that it can be copied to generate compatible vectors for the nullspace.
        null_space = self.build_nullspace(self.function_space, u.vector())
        # Attach near nullspace to matrix
        as_backend_type(A).set_near_nullspace(null_space)

        # Create PETSC smoothed aggregation AMG preconditioner and attach near null space
        pc = PETScPreconditioner("petsc_amg")

        # Use Chebyshev smoothing for multigrid
        PETScOptions.set("mg_levels_ksp_type", "chebyshev")
        PETScOptions.set("mg_levels_pc_type", "jacobi")

        # Improve estimate of eigenvalues for Chebyshev smoothing
        PETScOptions.set("mg_levels_esteig_ksp_type", "cg")
        PETScOptions.set("mg_levels_ksp_chebyshev_esteig_steps", 50)

        # Create CG Krylov solver and turn convergence monitoring on
        solver = PETScKrylovSolver("cg", pc)
        solver.parameters["monitor_convergence"] = True

        # Set matrix operator
        solver.set_operator(A)

        # Compute solution
        solver.solve(u.vector(), b)
        
        return u
    
    def build_nullspace(self, V, x):
        """Function to build null space for 2D and 3D elasticity"""

        # Create list of vectors for null space
        if self.dimension == 3:
            nullspace_basis = [x.copy() for i in range(6)]
            # Build translational null space basis
            V.sub(0).dofmap().set(nullspace_basis[0], 1.0);
            V.sub(1).dofmap().set(nullspace_basis[1], 1.0);
            V.sub(2).dofmap().set(nullspace_basis[2], 1.0);

            # Build rotational null space basis
            V.sub(0).set_x(nullspace_basis[3], -1.0, 1);
            V.sub(1).set_x(nullspace_basis[3],  1.0, 0);
            V.sub(0).set_x(nullspace_basis[4],  1.0, 2);
            V.sub(2).set_x(nullspace_basis[4], -1.0, 0);
            V.sub(2).set_x(nullspace_basis[5],  1.0, 1);
            V.sub(1).set_x(nullspace_basis[5], -1.0, 2);
        elif self.dimension == 2:
            nullspace_basis = [x.copy() for i in range(3)]
            V.sub(0).set_x(nullspace_basis[2], -1.0, 1);
            V.sub(1).set_x(nullspace_basis[2], 1.0, 0);
        else:
            raise Exception('only 2D or 3D is supported by nullspace')

        for x in nullspace_basis:
            x.apply("insert")

        # Create vector space basis and orthogonalize
        basis = VectorSpaceBasis(nullspace_basis)
        basis.orthonormalize()

        return basis