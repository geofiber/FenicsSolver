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

"""
Features:
- transient: support very slow boundary change, Elastostatics
- thermal stress are implemented but with very basic example
- modal analysis, not tested yet
- boundary conditions: see member funtion `update_boundary_conditions()`
- support 2D and 3D with nullspace accel

Todo:
- Elastodynamics - or dynamics (acceleration * density can not be ignored) vibration, damping,
- point source, as nodal constraint,  is not supported yet
- contact/frictional boundary condition, not yet implemented
- nonhomogenous meterial property like elastic modulus, not yet tested
- anisotropy needs rank 4 tensor, not yet tested

plasticity will be implemented in PlasticitySolver,
and other nonliearity by NonlinearElasticitySolver
"""

from __future__ import print_function, division
import math
import collections
import numbers
import numpy as np


#####################################
from dolfin import *

from .SolverBase import SolverBase, SolverError
class LinearElasticitySolver(SolverBase):
    # static and dynamic 
    def __init__(self, case_settings):
        case_settings['vector_name'] = 'displacement'
        SolverBase.__init__(self, case_settings)
        # solver specific setting
        self.solving_modal = False
        self.solving_dynamics = False  # not quasi-static,  structure's acceleration make impact

    def sigma(self, u):
        # Stress computation for linear elasticity
        elasticity = self.material['elastic_modulus']
        nu = self.material['poisson_ratio']
        mu = elasticity/(2.0*(1.0 + nu))
        lmbda = elasticity*nu/((1.0 + nu)*(1.0 - 2.0*nu))
        #return 2.0*mu*sym(grad(u)) + lmbda*tr(sym(grad(u)))*Identity(len(u))  # div(u) == tr(sym(grad(u)))?
        return 2.0*mu*sym(grad(u)) + lmbda*div(u)*Identity(len(u))

    def von_Mises(self, u):
        s = self.sigma(u) - (1./3)*tr(self.sigma(u))*Identity(self.dimension)  # deviatoric stress
        von_Mises = sqrt(3./2*inner(s, s))

        V = FunctionSpace(self.mesh, 'P', 1)  # correct, but why using another function space
        return project(von_Mises, V)

    def thermal_stress(self, T):
        elasticity = self.material['elastic_modulus']
        nu = self.material['poisson_ratio']
        tec = self.material['thermal_expansion_coefficient']
        thermal_strain = tec * ( T - Constant(self.reference_values['temperature']))
        #(lmbda*tr(eps(v)) - kappa*dT)*Identity(2) + 2*mu*eps(v)  # coupled
        #alpha*(3*lmbda+2*mu)*dT)*Identity(2)  # weak coupled
        return elasticity/(1.0 - 2.0*nu) * thermal_strain * Identity(self.dimension)

    def strain_energy(self, u):
        # Strain energy or the plastic heat generation
        elasticity = self.material['elastic_modulus']
        nu = self.material['poisson_ratio']
        mu = elasticity/(2.0*(1.0 + nu))
        lmbda = elasticity*nu/((1.0 + nu)*(1.0 - 2.0*nu))
        return lmbda/2.0*(tr(eps(v)))^2 + mu*tr(eps(v)**2)

    def get_flux(self, u, mag_vector):
        # pass-through , to be overloaded in large deformation solver
        return mag_vector

    def update_boundary_conditions(self, time_iter_, u, v, ds):
        V = self.function_space
        bcs = []
        integrals_N = []
        mesh_normal = FacetNormal(self.mesh)  # n is predefined as outward as positive

        if 'point_source' in self.settings and self.settings['point_source']:
            ps = self.settings['surface_source']
            #assume it s PointSource type, or a list of PointSource
            bcs.append(ps)

        if 'surface_source' in self.settings and self.settings['surface_source']:
            gS = self.get_flux(u, self.settings['surface_source']['value'])
            if 'direction' in self.settings['surface_source'] and self.settings['surface_source']['direction']:
                direction_vector = self.settings['surface_source']['direction']
            else:
                integrals_N.append(dot(mesh_normal*gS, v)*ds)

        for name, bc_settings in self.boundary_conditions.items():
            i = bc_settings['boundary_id']
            bc = self.get_boundary_variable(bc_settings)

            print(bc)
            if bc['type'] =='Dirichlet' or bc['type'] =='displacement':
                if not self.is_mixed_function_space:
                    bv = bc['value']  # translate_value() is not supported for value types: [1e-3, None, None]
                    if isinstance(bv, (tuple, list)) and len(bv) == self.dimension:
                        axis_i=0
                        for disp in bv:
                            if not disp is None:  # None means free of constraint, but zero is kind of constraint
                                dbc = DirichletBC(V.sub(axis_i), self.translate_value(disp), self.boundary_facets, i)
                                bcs.append(dbc)
                            axis_i += 1
                    else:
                        dbc = DirichletBC(V, self.translate_value(bv), self.boundary_facets, i)
                        bcs.append(dbc)
                else: # mixed_function_space for LargeDeformationSolver
                    disp_i, vel_i, pressure_i = 0, 1, 2
                    if 'value' in bc:  # only displacement is provided, set the velocity as zero?
                        bv = bc['value']
                        if bc['variable'] == 'displacement':
                            if isinstance(bv, (tuple, list)) and len(bv) == self.dimension and not all(bv):
                                axis_i=0
                                for disp in bv:
                                    if not disp is None:  # None means free of constraint, but zero is kind of constraint
                                        dbc = DirichletBC(V.sub(disp_i).sub(axis_i), self.translate_value(disp), self.boundary_facets, i)
                                        bcs.append(dbc)
                                    axis_i += 1
                            else:
                                var_i = disp_i
                                dbc = DirichletBC(V.sub(var_i), self.translate_value(bv), self.boundary_facets, i)
                                bcs.append(dbc)
                        elif bc['variable'] == 'velocity':
                            var_i = vel_i
                            dbc = DirichletBC(V.sub(var_i), self.translate_value(bv), self.boundary_facets, i)
                            bcs.append(dbc)
                        elif bc['variable'] == 'all':
                            dbc = DirichletBC(V, self.translate_value(bv), self.boundary_facets, i)
                            bcs.append(dbc)
                        else:
                            print(bc)
                            raise SolverError('Error, boundary setting is not supported: ')

                    else: # bc['values'] =[ {'variable': displacement' , 'value': dvalue}, { 'variable':'velocity', 'value': vvalue}
                        pass  # not yet needed

            elif bc['type'] == 'force':
                if isinstance(bc['value'], (tuple, list)) and len(bc['value']) == self.dimension:
                    force_vector = Constant((bc['value']))
                else:
                    bc_force = self.translate_value(bc['value'])
                    # calc the surface area and calc stress, normal and tangential?
                    bc_area = assemble(Constant(1)*ds(bc['boundary_id'], domain=self.mesh))
                    print('boundary area (m2) for force boundary is', bc_area)
                    g = bc_force / bc_area
                    # FIXME: assuming all force are normal to mesh boundary
                    if 'direction' in bc and bc['direction']:
                        direction_vector = bc['direction']
                    else:
                        direction_vector = mesh_normal
                    force_vector = direction_vector*g
                integrals_N.append(dot(self.get_flux(u, force_vector), v)*ds(i))
            elif bc['type'] == 'pressure':
                # scalar, normal to boundary surface, or by a given direction vector
                if 'direction' in bc and bc['direction']:
                    direction_vector = bc['direction']
                else:
                    direction_vector = mesh_normal
                g = direction_vector * self.translate_value(bc['value'])
                #FIXME: assuming all force are normal to mesh boundary
                integrals_N.append(dot(self.get_flux(u, g),v)*ds(i))
            elif bc['type'] == 'stress':  # must be normal stress vector, or stress tensor
                g = self.translate_value(bc['value'])
                if isinstance(g, (Constant, )):
                    pass
                else:
                    g = dot(g, mesh_normal)  # FIXME: FSI reverse direction before get here
                integrals_N.append(dot(self.get_flux(u, g),v)*ds(i))
            elif bc['type'] == 'Neumann':  # Neumann is the strain: du/dx then how to make a surface stress?
                raise SolverError('Neumann boundary type`{}` is not supported'.format(bc['type']))
            elif bc['type'] == 'symmetry':
                raise SolverError('symmetry boundary type`{}` is not supported'.format(bc['type']))
            else:
                raise SolverError('boundary type`{}` is not supported'.format(bc['type']))
        ## nodal constraint is not yet supported, try make it a small surface load instead
        return bcs, integrals_N

    def generate_form(self, time_iter_, u, v, u_current, u_prev):
        # todo: transient
        V = self.function_space

        elasticity = self.material['elastic_modulus']
        nu = self.material['poisson_ratio']
        mu = elasticity/(2.0*(1.0 + nu))
        lmbda = elasticity*nu/((1.0 + nu)*(1.0 - 2.0*nu))

        F = inner(self.sigma(u), grad(v))*dx
        if self.transient_settings['transient'] and self.solving_dynamics:
            if time_iter_>=1:
                accel = self.get_acceleration(time_iter_)
                #mesh_velocity in FSI
                F -= self.material['density'] * inner(accel, v) * dx

        ds= Measure("ds", subdomain_data=self.boundary_facets)  # if later marking updating in this ds?
        bcs, integrals_F = self.update_boundary_conditions(time_iter_, u, v, ds)
        if time_iter_==0:
            plot(self.boundary_facets, title = "boundary facets colored by ID")

        if self.body_source:
            integrals_F.append( inner(self.body_source, v)*dx )

        # thermal stress
        if not hasattr(self, 'temperature_distribution'):
            if 'temperature_distribution' in self.settings and self.settings['temperature_distribution']:
                self.temperature_distribution = self.translate_value(self.settings['temperature_distribution'])
        if hasattr(self, 'temperature_distribution') and self.temperature_distribution:
            T = self.translate_value(self.temperature_distribution)  # interpolate
            stress_t = self.thermal_stress(self.settings['temperature_distribution'])
            if stress_t:
                F -= inner(stress_t, grad(v)) * dx
                # sym(grad(v)) == epislon(v), it does not matter for multiply identity matrix

        # Assemble system, applying boundary conditions and extra items
        if len(integrals_F):
            for item in integrals_F: F += item  # L side

        return F, bcs

    def solve_form(self, F, u_, bcs):
        if self.dimension == 3:
            u_ = self.solve_amg(F, u_, bcs)
        else:
            u_ = self.solve_linear_problem(F, u_, bcs)
        # calc boundingbox to make sure no large deformation?
        return u_

    def displacement(self):
        if self.is_mixed_function_space:
            raise SolverError('subclass with mixed_function_space must override this function')
        else:
            return self.w_current

    def velocity(self):
        dt = self.get_time_step(self.current_step)
        if self.is_mixed_function_space:
            raise SolverError('subclass with mixed_function_space must override this function')
        else:
            u_ = self.w_current
            u0_ = self.w_prev
            return (u_ - u0_)/Constant(dt)

    def solve_modal(self):
        # FIXME: not yet complete code, mass matrix is not added into form, also only for linear form
        trial_function = TrialFunction(self.function_space)
        test_function = TestFunction(self.function_space)
        # Define functions for transient loop
        u_current = self.get_initial_field()  # init to default or user provided constant
        u_prev = Function(self.function_space)
        u_prev.assign(u_current)

        current_step = 0
        F, bcs = self.generate_form(current_step, trial_function, test_function, u_current, u_prev)
        return self.solve_modal_form(F, bcs)

    def solve_modal_form(self, F, bcs):
        # Test for PETSc
        if not has_linear_algebra_backend("PETSc"):
            print("DOLFIN has not been configured with PETSc. Exiting.")
            exit()
        # Set backend to PETSC
        parameters["linear_algebra_backend"] = "PETSc"

        # todo: Assemble stiffness form, it is not fully tested yet
        A = PETScMatrix()
        b = PETScVector()
        assemble_system(lhs(F), rhs(F), bcs, A_tensor=A, b_tensor=b)  # preserve symmetry

        # Create eigensolver
        eigensolver = SLEPcEigenSolver(A)

        # Compute all eigenvalues of A x = \lambda x
        print("Computing eigenvalues. This can take a minute.")
        eigensolver.solve()

        # Extract largest (first) eigenpair
        r, c, rx, cx = eigensolver.get_eigenpair(0)

        print("Largest eigenvalue: ", r)

        # Initialize function and assign eigenvector
        ev = Function(self.function_space)
        ev.vector()[:] = rx

        return ev
