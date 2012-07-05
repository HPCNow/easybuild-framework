##
# Copyright 2009-2012 Stijn De Weirdt, Dries Verdegem, Kenneth Hoste, Pieter De Baets, Jens Timmerman
#
# This file is part of EasyBuild,
# originally created by the HPC team of the University of Ghent (http://ugent.be/hpc).
#
# http://github.com/hpcugent/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
from distutils.version import LooseVersion
import fileinput
import glob
import re
import os
import shutil
import sys
from easybuild.framework.application import Application
from easybuild.tools.filetools import run_cmd

class CP2K(Application):
    """
    Support for building CP2K
    - prepare module include files if required
    - generate custom config file in 'arch' directory
    - build CP2K
    - run regression test if desired
    - install by copying binary executables
    """

    def __init__(self, *args, **kwargs):
        Application.__init__(self, *args, **kwargs)

        self.cfg.update({'type':['popt',"Type of build ('popt' or 'psmp') (default: 'popt)"],
                         'typeopt':[True,"Enable optimization (default: True)"],
                         'libint':[True,"Use LibInt (default: True)"],
                         'modincprefix':['',"IMKL prefix for modinc include dir (default: '')"],                         
                         'modinc':[[],"List of modinc's to use (*.f90), or 'True' to use all found at given prefix (default: [])"],
                         'extracflags':['',"Extra CFLAGS to be added (default: '')"],
                         'extradflags':['',"Extra DFLAGS to be added (default: '')"],
                         'runtest':[True, 'Indicates if a regression test should be run after make (default: True)'],
                         'ignore_regtest_fails':[False, "Ignore failures in regression test (should be used with care) (default: False)."],
                         'maxtasks':[3, "Maximum number of CP2K instances run at the same time during testing (default: 3)"]
                         })

        self.typearch = None

        # this should be set to False for old versions of GCC (e.g. v4.1)
        self.compilerISO_C_BINDING = True

        # compiler options that need to be set in Makefile
        self.debug = ''
        self.fpic = ''

        self.libsmm = ''
        self.modincpath = ''
        self.openmp = ''

        self.make_instructions = ''

    def _generateMakefile(self, options):
        """Generate Makefile based on options dictionary and optional make instructions"""

        text = "# Makefile generated by CP2K._generateMakefile, items might appear in random order\n"
        for key, value in options.iteritems():
            text += "%s = %s\n" % (key, value)
        return text + self.make_instructions

    def configure(self):
        """Configure build
        - build Libint wrapper
        - generate Makefile
        """

        # set compilers options according to toolkit config
        ## full debug: -g -traceback -check all -fp-stack-check
        ## -g links to mpi debug libs
        if self.tk.opts['debug']:
            self.debug = '-g'
            self.log.info("Debug build")
        if self.tk.opts['pic']:
            self.fpic = "-fPIC"
            self.log.info("Using fPIC")

        # report on extra flags being used
        if self.getcfg('extracflags'):
            self.log.info("Using extra CFLAGS: %s" % self.getcfg('extracflags'))
        if self.getcfg('extradflags'):
            self.log.info("Using extra CFLAGS: %s" % self.getcfg('extradflags'))

        # libsmm support
        if os.environ.has_key('SOFTROOTLIBSMM'):
            libsmms = glob.glob(os.path.join(os.environ['SOFTROOTLIBSMM'], 'lib') + '/libsmm_*nn.a')
            moredflags = ' ' + ' '.join([os.path.basename(os.path.splitext(x)[0]).replace('lib', '-D__HAS_') for x in libsmms])
            self.updatecfg('extradflags', moredflags)
            self.libsmm = ' '.join(libsmms)
            self.log.debug('Using libsmm %s (extradflags %s)' % (self.libsmm, moredflags))

        # obtain list of modinc's to use
        if self.getcfg("modinc"):
            self.modincpath = self.prepmodinc()

        # set typearch
        self.typearch = "Linux-x86-64-%s" % self.tk.name

        # extra make instructions
        self.make_instructions = "graphcon.o: graphcon.F\n\t$(FC) -c $(FCFLAGS2) $<\n"

        # compiler toolkit specific configuration
        comp_fam = self.tk.toolkit_comp_family()
        if comp_fam == "Intel":
            options = self.configureIntelBased()
        elif comp_fam == "GCC":
            options = self.configureGCCBased()
        else:
            self.log.error("Don't know how to tweak configuration for compiler used.")

        if os.getenv('SOFTROOTIMKL'):
            options = self.configureMKL(options)
        elif os.getenv('SOFTROOTACML'):
            options = self.configureACML(options) 
        elif os.getenv('SOFTROOTATLAS'):
            options = self.configureATLAS(options) 

        if os.getenv('SOFTROOTFFTW'):
            options = self.configureFFTW(options)

        if os.getenv('SOFTROOTLAPACK'):
            options = self.configureLAPACK(options)

        if os.getenv('SOFTROOTSCALAPACK'):
            options = self.configureScaLAPACK(options)

        # avoid group nesting
        options['LIBS'] = options['LIBS'].replace('-Wl,--start-group','').replace('-Wl,--end-group','')

        options['LIBS'] = "-Wl,--start-group %s -Wl,--end-group" % options['LIBS']

        # create arch file using options set
        archfile = os.path.join(self.getcfg('startfrom'), 'arch', 
                                '%s.%s' % (self.typearch, self.getcfg('type')))
        try:
            txt = self._generateMakefile(options)
            f = open(archfile, 'w')
            f.write(txt)
            f.close()
            self.log.info("Content of makefile (%s):\n%s" % (archfile, txt))
        except IOError, err:
            self.log.error("Writing makefile %s failed: %s" % (archfile, err))

    def prepmodinc(self):
        """Prepare list of module files"""

        self.log.debug("Preparing module files")

        softrootimkl = os.getenv('SOFTROOTIMKL')

        if softrootimkl:

            ## prepare modinc target path
            modincpath = os.path.join(self.builddir, 'modinc')
            self.log.debug("Preparing module files in %s" % modincpath)

            try:
                os.mkdir(modincpath)
            except OSError, err:
                self.log.error("Failed to create directory for module include files: %s" % err)

            ## get list of modinc source files
            modincdir = os.path.join(softrootimkl, self.getcfg("modincprefix"), 'include')

            if type(self.getcfg("modinc")) == list:
                modfiles = [os.path.join(modincdir, x) for x in self.getcfg("modinc")]

            elif type(self.getcfg("modinc")) == bool and type(self.getcfg("modinc")):
                modfiles = glob.glob(os.path.join(modincdir, '*.f90'))

            else:
                self.log.error("prepmodinc: Please specify either a boolean value " \
                               "or a list of files in modinc (found: %s)." % 
                               self.getcfg("modinc"))

            f77 = os.getenv('F77')
            if not f77:
                self.log.error("F77 environment variable not set, can't continue.")

            ## create modinc files
            for f in modfiles:
                if f77.endswith('ifort') :
                    cmd = "%s -module %s -c %s" % (f77, modincpath, f)
                elif f77 in ['gfortran', 'mpif77'] :
                    cmd = "%s -J%s -c %s" % (f77, modincpath, f)
                else:
                    self.log.error("prepmodinc: Unknown value specified for F77 (%s)" % f77)

                run_cmd(cmd, log_all=True, simple=True)

            return modincpath
        else:
            self.log.error("Don't know how to prepare modinc, IMKL not found")

    def configureCommon(self):
        """Common configuration for all toolkits"""

        # openmp introduces 2 major differences
        ## -automatic is default: -noautomatic -auto-scalar
        ## some mem-bandwidth optimisation
        if self.getcfg('type') == 'psmp':
            self.openmp = self.tk.get_openmp_flag()

        # determine which opt flags to use
        if self.getcfg('typeopt'):
            optflags = 'OPT'
            regflags = 'OPT2'
        else:
            optflags = 'NOOPT'
            regflags = 'NOOPT'

        # make sure a MPI-2 able MPI lib is used
        mpi2libs = ['impi', 'MVAPICH2', 'OpenMPI']
        mpi2 = False
        for mpi2lib in mpi2libs:
            if os.getenv('SOFTROOT%s' % mpi2lib.upper()):
                mpi2 = True
            else:
                self.log.debug("MPI-2 supporting MPI library %s not loaded.")
        
        if not mpi2:
            self.log.error("CP2K needs MPI-2, no known MPI-2 supporting library loaded?")

        options = {
            'CC': os.getenv('MPICC'),
            'CPP': '',

            'FC': '%s %s' % (os.getenv('MPIF77'), self.openmp),
            'LD': '%s %s' % (os.getenv('MPIF77'), self.openmp),
            'AR': 'ar -r',

            'CPPFLAGS': '',
            
            'FPIC': self.fpic,
            'DEBUG': self.debug,

            'FCFLAGS': '$(FCFLAGS%s)' % optflags,
            'FCFLAGS2': '$(FCFLAGS%s)' % regflags,

            'CFLAGS' : ' %s %s $(FPIC) $(DEBUG) %s ' % (os.getenv('SOFTVARCPPFLAGS'),
                                                        os.getenv('SOFTVARLDFLAGS'),
                                                        self.getcfg('extracflags')),
            'DFLAGS': ' -D__parallel -D__BLACS -D__SCALAPACK -D__FFTSG %s' % self.getcfg('extradflags'),

            'LIBS': os.getenv('LIBS'),

            'FCFLAGSNOOPT': '$(DFLAGS) $(CFLAGS) -O0  $(FREE) $(FPIC) $(DEBUG)',
            'FCFLAGSOPT': '-O2 $(FREE) $(SAFE) $(FPIC) $(DEBUG)',
            'FCFLAGSOPT2': '-O1 $(FREE) $(SAFE) $(FPIC) $(DEBUG)',
        }

        if self.getcfg('libint'):

            softrootlibint = os.getenv('SOFTROOTLIBINT')
            if not softrootlibint:
                self.log.error("LibInt module not loaded.")

            options['DFLAGS'] += ' -D__LIBINT'

            libintcompiler = "%s %s" % (os.getenv('CC'), os.getenv('CFLAGS'))

            # Build libint-wrapper, if required
            libint_wrapper = ''

            ## required for old versions of GCC
            if not self.compilerISO_C_BINDING:
                options['DFLAGS'] += ' -D__HAS_NO_ISO_C_BINDING'

                ## determine path for libint_tools dir
                libinttools_paths = ['libint_tools', 'tools/hfx_tools/libint_tools']
                libinttools_path = None
                for path in libinttools_paths:
                    path = os.path.join(self.getcfg('startfrom'), path)
                    if os.path.isdir(path):
                        libinttools_path = path
                        os.chdir(libinttools_path)
                if not libinttools_path:
                    self.log.error("No libinttools dir found")

                ## build libint wrapper
                cmd = "%s -c libint_cpp_wrapper.cpp -I%s/include" % (libintcompiler, softrootlibint)
                if not run_cmd(cmd, log_all=True, simple=True):
                    self.log.error("Building the libint wrapper failed")
                libint_wrapper = '%s/libint_cpp_wrapper.o' % libinttools_path

            # determine LibInt libraries based on major version number
            libint_maj_ver = os.getenv('SOFTVERSIONLIBINT').split('.')[0]
            if libint_maj_ver == '1':
                libint_libs = "$(LIBINTLIB)/libderiv.a $(LIBINTLIB)/libint.a $(LIBINTLIB)/libr12.a"
            elif libint_maj_ver == '2':
                libint_libs = "$(LIBINTLIB)/libint2.a"
            else:
                self.log.error("Don't know how to handle libint version %s" % libint_maj_ver)
            self.log.info("Using LibInt version %s" % (libint_maj_ver))

            options['LIBINTLIB'] = '%s/lib' % softrootlibint
            options['LIBS'] += ' -lstdc++ %s %s' % (libint_libs, libint_wrapper)

        return options

    def configureIntelBased(self):
        """Configure for Intel based toolkits"""

        options = self.configureCommon()

        extrainc = ''
        if self.modincpath:
            extrainc = '-I%s' % self.modincpath

        options.update({

            ## -Vaxlib : older options
            'FREE': '-fpp -free',

            #SAFE = -assume protect_parens -fp-model precise -ftz # problems
            'SAFE': '-assume protect_parens -no-unroll-aggressive',

            'INCFLAGS': '$(DFLAGS) -I$(INTEL_INC) -I$(INTEL_INCF) %s' % extrainc,

            'LDFLAGS': '$(INCFLAGS) -i-static',
            'OBJECTS_ARCHITECTURE': 'machine_intel.o',

        })

        options['DFLAGS'] += ' -D__INTEL'

        options['FCFLAGSOPT'] += ' $(INCFLAGS) -xHOST -heap-arrays 64 -funroll-loops'
        options['FCFLAGSOPT2'] += ' $(INCFLAGS) -xHOST -heap-arrays 64'

        # see http://software.intel.com/en-us/articles/build-cp2k-using-intel-fortran-compiler-professional-edition/
        self.make_instructions += "qs_vxc_atom.o: qs_vxc_atom.F\n\t$(FC) -c $(FCFLAGS2) $<\n"

        if LooseVersion(os.getenv('SOFTVERSIONIFORT')) >= LooseVersion("2011.8"):
            self.make_instructions += "et_coupling.o: et_coupling.F\n\t$(FC) -c $(FCFLAGS2) $<\n"
            self.make_instructions += "qs_vxc_atom.o: qs_vxc_atom.F\n\t$(FC) -c $(FCFLAGS2) $<\n"

        elif LooseVersion(os.getenv('SOFTVERSIONIFORT')) >= LooseVersion("2011"):
            self.log.error("CP2K won't build correctly with the Intel v12 compilers before version 2011.8.")

        return options

    def configureGCCBased(self):
        """Configure for GCC based toolkits"""
        options = self.configureCommon()

        options.update({

            ## need this to prevent "Unterminated character constant beginning" errors
            'FREE': '-ffree-form -ffree-line-length-none',

            'LDFLAGS': '$(FCFLAGS)',
            'OBJECTS_ARCHITECTURE': 'machine_gfortran.o',
        })

        options['DFLAGS'] += ' -D__GFORTRAN'

        options['FCFLAGSOPT'] += ' $(DFLAGS) $(CFLAGS) -march=native -ffast-math ' \
                                 '-funroll-loops -ftree-vectorize -fmax-stack-var-size=32768'
        options['FCFLAGSOPT2'] += ' $(DFLAGS) $(CFLAGS) -march=native'

        return options

    def configureACML(self, options):
        """Configure for AMD Math Core Library (ACML)"""

        openmp_suffix = ''
        if self.openmp:
            openmp_suffix = '_mp'

        options['ACML_INC'] = '%s/gfortran64%s/include' % (os.getenv('SOFTROOTACML'), openmp_suffix)
        options['CFLAGS'] += ' -I$(ACML_INC) -I$(FFTW_INC)'
        options['DFLAGS'] += ' -D__FFTACML'

        blas = os.getenv('LIBBLAS')
        blas = blas.replace('gfortran64', 'gfortran64%s' % openmp_suffix)
        options['LIBS'] += ' %s %s %s' % (self.libsmm, os.getenv('LIBSCALAPACK'), blas)

        return options

    def configureATLAS(self, options):
        """Configure for ATLAS"""

        options['LIBS'] += ' %s %s' % (self.libsmm, os.getenv('LIBBLAS'))

        return options

    def configureMKL(self, options):
        """Configure for Intel Math Kernel Library (MKL)"""

        options.update({
            'INTEL_INC': '$(MKLROOT)/include',
            'INTEL_INCF': '$(INTEL_INC)/fftw',
        })
        
        options['DFLAGS'] += ' -D__FFTW3 -D__FFTMKL'

        extra = ''
        if self.modincpath:
            extra = '-I%s' % self.modincpath
        options['CFLAGS'] += ' -I$(INTEL_INC) -I$(INTEL_INCF) %s $(FPIC) $(DEBUG)' % extra
        
        options['LIBS'] += ' %s %s' % (self.libsmm, os.getenv('LIBSCALAPACK'))

        return options

    def configureFFTW(self, options):
        """Configure for Fastest Fourier Transform in the West (FFTW)"""

        softroot = os.getenv('SOFTROOTFFTW')

        options.update({
                        'FFTW_INC': '%s/include' % softroot, # GCC
                        'FFTW3INC': '%s/include' % softroot, # Intel
                        'FFTW3LIB': '%s/lib' % softroot, # Intel
                        })

        options['DFLAGS'] += ' -D__FFTW3'

        options['LIBS'] += ' -lfftw3'

        return options

    def configureLAPACK(self, options):
        """Configure for LAPACK library"""

        options['LIBS'] += ' %s' % os.getenv('LIBLAPACK_MT')

        return options

    def configureScaLAPACK(self, options):
        """Configure for ScaLAPACK library"""

        options['LIBS'] += ' %s' % os.getenv('LIBSCALAPACK')

        return options

    def make(self):
        """Start the actual build
        - go into makefiles dir
        - patch Makefile
        - build
        """

        makefiles = os.path.join(self.getcfg('startfrom'), 'makefiles')
        try:
            os.chdir(makefiles)
        except:
            self.log.error("Can't change to makefiles dir %s: %s" % (makefiles))

        # modify makefile for parallel build
        parallel = self.getcfg('parallel')
        if parallel:

            try:
                for line in fileinput.input('Makefile', inplace=1, backup='.orig.patchictce'):
                    line = re.sub(r"^PMAKE\s*=.*$", "PMAKE\t= $(SMAKE) -j %s" % parallel, line)
                    sys.stdout.write(line)
            except IOError, err:
                self.log.error("Can't modify/write Makefile in %s: %s" % (makefiles, err))

        # update make options with MAKE
        self.updatecfg('makeopts', 'MAKE="make -j %s" all' % self.getcfg('parallel'))

        # update make options with ARCH and VERSION
        self.updatecfg('makeopts', 'ARCH=%s VERSION=%s' % (self.typearch, self.getcfg('type')))

        cmd = "make %s" % self.getcfg('makeopts')

        # clean first
        run_cmd(cmd + " clean", log_all=True, simple=True, log_output=True)

        # build
        run_cmd(cmd, log_all=True, simple=True, log_output=True)

    def test(self):
        """Run regression test."""

        if self.getcfg('runtest'):

            # change to root of build dir
            try:
                os.chdir(self.builddir)
            except OSError, err:
                self.log.error("Failed to change to %s: %s" % self.builddir)

            # use regression test reference output if available
            ## try and find an unpacked directory that starts with 'LAST-'
            regtest_refdir = None
            for d in os.listdir(self.builddir):
                if d.startswith("LAST-"):
                    regtest_refdir = d
                    break

            # location of do_regtest script
            regtest_script = "%s/cp2k/tools/do_regtest" % self.builddir

            # patch do_regtest so that reference output is used
            if regtest_refdir:
                self.log.info("Using reference output available in %s" % regtest_refdir)
                try:
                    for line in fileinput.input(regtest_script, inplace=1, backup='.orig.refout'):
                        line = re.sub(r"^(dir_last\s*=\${dir_base})/.*$", r"\1/%s" % regtest_refdir, line)
                        sys.stdout.write(line)
                except IOError, err:
                    self.log.error("Failed to modify '%s': %s" % (regtest_script, err))

            else:
                self.log.info("No reference output found for regression test, just continuing without it...")

            # configure regression test
            cfg_txt="""FORT_C_NAME="%(f90)s"
dir_base=%(base)s
cp2k_version=%(cp2k_version)s
dir_triplet=%(triplet)s
leakcheck="YES"
maxtasks=%(maxtasks)s
            """ % {'f90':os.getenv('F90'),
                   'base':self.builddir,
                   'cp2k_version':self.getcfg('type'),
                   'triplet':self.typearch,
                   'maxtasks':self.getcfg('maxtasks')
                  }

            cfg_fn = "cp2k_regtest.cfg"

            try:
                f= open(cfg_fn, "w")
                f.write(cfg_txt)
                f.close()
            except IOError, err:
                self.log.error("Failed to create config file %s: %s" % (cfg_fn, err))

            # run regression test
            cmd = "%s -nocvs -quick -nocompile -config %s" % (regtest_script, cfg_fn)

            (regtest_output, ec) = run_cmd(cmd, log_all=True, simple=False, log_output=True)

            if ec == 0:
                self.log.info("Regression test output:\n%s" % regtest_output)
            else:
                self.log.error("Regression test failed (non-zero exit code): %s" % regtest_output)

            # pattern to search for regression test summary
            re_pattern = "^number\s+of\s+%s\s+tests\s+(?P<cnt>[0-9]+)"

            # find total number of tests
            regexp = re.compile(re_pattern % "", re.M)
            res = regexp.search(regtest_output)
            tot_cnt = None
            if res:
                tot_cnt = int(res.group('cnt'))
            else:
                self.log.error("Finding total number of tests in regression$G:q test summary failed")
            msg = "Regression test reported %%s / %s %%s tests" % tot_cnt

            # function to report on regtest results
            def test_report(test_result):
                """Report on tests with given result."""

                postmsg = ''

                test_result = test_result.upper()
                regexp = re.compile(re_pattern % test_result, re.M)

                cnt = None
                res = regexp.search(regtest_output)
                if not res:
                    self.log.error("Finding number of %s tests in regression test summary failed" % test_result.lower())
                else:
                    cnt = int(res.group('cnt'))

                logmsg = msg % (cnt, test_result.lower())

                # failed tests indicate problem with installation
                # wrong tests are only an issue when there are excessively many
                if (test_result == "FAILED" and cnt > 0) or (test_result == "WRONG" and (cnt / tot_cnt) > 0.1):
                    if self.getcfg('ignore_regtest_fails'):
                        self.log.warning(logmsg)
                        self.log.info("Ignoring failures in regression test, as requested.")
                    else:
                        self.log.error(logmsg)
                elif test_result == "CORRECT" or cnt == 0:
                    self.log.info(logmsg)
                else:
                    self.log.warning(logmsg)

                return postmsg

            # number of failed/wrong tests, will report error if count is positive
            self.postmsg += test_report("FAILED")
            self.postmsg += test_report("WRONG")

            # number of new tests, will be high if a non-suitable regtest reference was used
            ## will report error if count is positive (is that what we want?)
            self.postmsg += test_report("NEW")

            # number of correct tests: just report
            test_report("CORRECT")

    def make_install(self):
        """Install built CP2K
        - copy from exe to bin
        - copy tests
        """

        # copy executables
        targetdir = os.path.join(self.installdir, 'bin')
        exedir = os.path.join(self.getcfg('startfrom'), 'exe/%s' % self.typearch)
        try:
            if not os.path.exists(targetdir):
                os.makedirs(targetdir)
            os.chdir(exedir)
            for exefile in os.listdir(exedir):
                if os.path.isfile(exefile):
                    shutil.copy2(exefile, targetdir)
        except OSError, err:
            self.log.error("Copying executables from %s to bin dir %s failed: %s" % (exedir, 
                                                                                     targetdir, 
                                                                                     err) )

        # copy tests
        srctests = os.path.join(self.getcfg('startfrom'), 'tests')
        targetdir = os.path.join(self.installdir, 'tests')
        if os.path.exists(targetdir):
            self.log.info("Won't copy tests. Destination directory %s already exists" % targetdir)
        else:
            try:
                shutil.copytree(srctests, targetdir)
            except:
                self.log.error("Copying tests from %s to %s failed" % (srctests, targetdir))

        # copy regression test results
        if self.getcfg('runtest'):
            try:
                for d in os.listdir(self.builddir):
                    if d.startswith('TEST-%s-%s' % (self.typearch, self.getcfg('type'))):
                        path = os.path.join(self.builddir, d)
                        target = os.path.join(self.installdir, d)
                        shutil.copytree(path, target)
                        self.log.info("Regression test results dir %s copied to %s" % (d, self.installdir))
                        break
            except (OSError, IOError), err:
                self.log.error("Failed to copy regression test results dir: %s" % err)

    def sanitycheck(self):
        """Custom sanity check for CP2K"""

        if not self.getcfg('sanityCheckPaths'):
            cp2k_type = self.getcfg('type')
            self.setcfg('sanityCheckPaths',{'files':["bin/%s.%s" % (x, cp2k_type) for x in ["cp2k",
                                                                                            "cp2k_shell",
                                                                                            "fes"]],
                                            'dirs':["tests"]
                                           })

            self.log.info("Customized sanity check paths: %s" % self.getcfg('sanityCheckPaths'))

        Application.sanitycheck(self)