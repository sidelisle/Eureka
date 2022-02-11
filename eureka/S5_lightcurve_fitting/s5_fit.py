import numpy as np
import matplotlib.pyplot as plt
import glob, os
import time as time_pkg
from ..lib import manageevent as me
from ..lib import readECF as rd
from ..lib import sort_nicely as sn
from ..lib import util, logedit
from . import parameters as p
from . import lightcurve as lc
from . import models as m
from .utils import get_target_data

#FINDME: Keep reload statements for easy testing
from importlib import reload
reload(p)
reload(m)
reload(lc)

class MetaClass:
    '''A class to hold Eureka! metadata.
    '''

    def __init__(self):
        return

def fitJWST(eventlabel, s4_meta=None):
    '''Fits 1D spectra with various models and fitters.

    Parameters
    ----------
    eventlabel: str
        The unique identifier for these data.
    s4_meta:    MetaClass
        The metadata object from Eureka!'s S4 step (if running S4 and S5 sequentially).

    Returns
    -------
    meta:   MetaClass
        The metadata object with attributes added by S5.

    Notes
    -------
    History:

    - November 12-December 15, 2021 Megan Mansfield
        Original version
    - December 17-20, 2021 Megan Mansfield
        Connecting S5 to S4 outputs
    - December 17-20, 2021 Taylor Bell
        Increasing connectedness of S5 and S4
    '''
    print("\nStarting Stage 5: Light Curve Fitting\n")

    # Initialize a new metadata object
    meta = MetaClass()
    meta.eventlabel = eventlabel

    # Load Eureka! control file and store values in Event object
    ecffile = 'S5_' + eventlabel + '.ecf'
    ecf = rd.read_ecf(ecffile)
    rd.store_ecf(meta, ecf)

    # load savefile
    if s4_meta == None:
        s4_meta = read_s4_meta(meta)

    meta = load_general_s4_meta_info(meta, s4_meta)

    if (not meta.s4_allapers) or (not meta.allapers):
        # The user indicated in the ecf that they only want to consider one aperture
        # in which case the code will consider only the one which made s4_meta.
        # Alternatively, S4 was run without allapers, so S5's allapers will only conside that one
        meta.spec_hw_range = [meta.spec_hw,]
        meta.bg_hw_range = [meta.bg_hw,]

    if meta.testing_S5:
        # Only fit a single channel while testing
        chanrng = [0]
    else:
        chanrng = range(meta.nspecchan)

    # Create directories for Stage 5 outputs
    meta.runs_s5 = []
    for spec_hw_val in meta.spec_hw_range:
        for bg_hw_val in meta.bg_hw_range:
            for channel in chanrng:
                run = util.makedirectory(meta, 'S5', ap=spec_hw_val, bg=bg_hw_val, ch=channel)
                meta.runs_s5.append(run)

    run_i = 0
    old_meta = meta
    for spec_hw_val in meta.spec_hw_range:
        for bg_hw_val in meta.bg_hw_range:

            t0 = time_pkg.time()

            meta = load_specific_s4_meta_info(old_meta, run_i, spec_hw_val, bg_hw_val)
            run_i += 1

            run_j = 0
            for channel in chanrng:
                # Get the directory for Stage 5 processing outputs
                meta.outputdir = util.pathdirectory(meta, 'S5', meta.runs_s5[run_j], ap=spec_hw_val, bg=bg_hw_val, ch=channel)
                run_j += 1

                # Copy existing S4 log file and resume log
                meta.s5_logname  = meta.outputdir + 'S5_' + meta.eventlabel + ".log"
                log         = logedit.Logedit(meta.s5_logname, read=meta.s4_logname)
                log.writelog("\nStarting Channel {} of {}\n".format(channel+1, meta.nspecchan))
                log.writelog(f"Input directory: {meta.inputdir}")
                log.writelog(f"Output directory: {meta.outputdir}")

                # Copy ecf (and update outputdir in case S5 is being called sequentially with S4)
                log.writelog('Copying S5 control file')
                # shutil.copy(ecffile, meta.outputdir)
                new_ecfname = meta.outputdir + ecffile.split('/')[-1]
                with open(new_ecfname, 'w') as new_file:
                    with open(ecffile, 'r') as file:
                        for line in file.readlines():
                            if len(line.strip())==0 or line.strip()[0]=='#':
                                new_file.write(line)
                            else:
                                line_segs = line.strip().split()
                                if line_segs[0]=='inputdir':
                                    new_file.write(line_segs[0]+'\t\t/'+meta.inputdir+'\t'+' '.join(line_segs[2:])+'\n')
                                else:
                                    new_file.write(line)

                # Set the intial fitting parameters
                params = p.Parameters(param_file=meta.fit_par)

                # Subtract off the zeroth time value to avoid floating point precision problems when fitting for t0
                offset = int(np.floor(meta.time[0]))
                time_offset = meta.time - offset
                if 't0' in params.dict.keys():
                    params.t0.value -= offset
                    params.t0.mn -= offset
                    params.t0.mx -= offset
                if 't_secondary' in params.dict.keys():
                    params.t_secondary.value -= offset
                    params.t_secondary.mn -= offset
                    params.t_secondary.mx -= offset
                for i in [0,2,3]:
                    # Skip col 1 which is the free/fixed/etc. column
                    if 't0' in params.dict.keys():
                        params.dict['t0'][i] -= offset
                    if 't_secondary' in params.dict.keys():
                        params.dict['t_secondary'][i] -= offset
                
                # Get the flux and error measurements for the current channel
                flux = meta.lcdata[channel,:]
                flux_err = meta.lcerr[channel,:]

                # Normalize flux and uncertainties to avoid large flux values
                flux_err /= np.ma.mean(flux)
                flux /= np.ma.mean(flux)

                if meta.testing_S5:
                    # FINDME: Use this area to add systematics into the data
                    # when testing new systematics models. In this case, I'm
                    # introducing an exponential ramp to test m.ExpRampModel().
                    log.writelog('****Adding exponential ramp systematic to light curve****')
                    fakeramp = m.ExpRampModel(parameters=params, name='ramp', fmt='r--')
                    fakeramp.coeffs = np.array([-1,40,-3, 0, 0, 0])
                    flux *= fakeramp.eval(time=time_offset)

                # Load the relevant values into the LightCurve model object
                lc_model = lc.LightCurve(time_offset, flux, channel, meta.nspecchan, log, unc=flux_err, name=eventlabel, time_units=f'Time = {meta.time_units} - {offset}')

                # Make the astrophysical and detector models
                modellist=[]
                if 'transit' in meta.run_myfuncs:
                    t_model = m.TransitModel(parameters=params, name='transit', fmt='r--')
                    modellist.append(t_model)
                if 'polynomial' in meta.run_myfuncs:
                    t_polynom = m.PolynomialModel(parameters=params, name='polynom', fmt='r--')
                    modellist.append(t_polynom)
                if 'expramp' in meta.run_myfuncs:
                    t_ramp = m.ExpRampModel(parameters=params, name='ramp', fmt='r--')
                    modellist.append(t_ramp)
                model = m.CompositeModel(modellist)

                # Fit the models using one or more fitters
                log.writelog("=========================")
                if 'lsq' in meta.fit_method:
                    log.writelog("Starting lsq fit.")
                    model.fitter = 'lsq'
                    lc_model.fit(model, meta, log, fitter='lsq')
                    log.writelog("Completed lsq fit.")
                    log.writelog("-------------------------")
                if 'emcee' in meta.fit_method:
                    log.writelog("Starting emcee fit.")
                    model.fitter = 'emcee'
                    lc_model.fit(model, meta, log, fitter='emcee')
                    log.writelog("Completed emcee fit.")
                    log.writelog("-------------------------")
                if 'dynesty' in meta.fit_method:
                    log.writelog("Starting dynesty fit.")
                    model.fitter = 'dynesty'
                    lc_model.fit(model, meta, log, fitter='dynesty')
                    log.writelog("Completed dynesty fit.")
                    log.writelog("-------------------------")
                if 'lmfit' in meta.fit_method:
                    log.writelog("Starting lmfit fit.")
                    model.fitter = 'lmfit'
                    lc_model.fit(model, meta, log, fitter='lmfit')
                    log.writelog("Completed lmfit fit.")
                    log.writelog("-------------------------")
                log.writelog("=========================")

                # Plot the results from the fit(s)
                if meta.isplots_S5 >= 1:
                    lc_model.plot(meta)
                
                log.closelog()

            # Calculate total time
            total = (time_pkg.time() - t0) / 60.
            print('\nTotal time (min): ' + str(np.round(total, 2)))

    return meta, lc_model

def read_s4_meta(meta):

    # Search for the S2 output metadata in the inputdir provided in
    # First just check the specific inputdir folder
    rootdir = os.path.join(meta.topdir, *meta.inputdir.split(os.sep))
    if rootdir[-1]!='/':
        rootdir += '/'
    fnames = glob.glob(rootdir+'S4_'+meta.eventlabel+'*_Meta_Save.dat')
    if len(fnames)==0:
        # There were no metadata files in that folder, so let's see if there are in children folders
        fnames = glob.glob(rootdir+'**/S4_'+meta.eventlabel+'*_Meta_Save.dat', recursive=True)

    if len(fnames)>=1:
        # get the folder with the latest modified time
        fname = max(fnames, key=os.path.getmtime)

    if len(fnames)==0:
        # There may be no metafiles in the inputdir - raise an error and give a helpful message
        raise AssertionError('Unable to find an output metadata file from Eureka!\'s S4 step '
                            +'in the inputdir: \n"{}"!'.format(rootdir))
    elif len(fnames)>1:
        # There may be multiple runs - use the most recent but warn the user
        print('WARNING: There are multiple metadata save files in your inputdir: \n"{}"\n'.format(rootdir)
                +'Using the metadata file: \n{}\n'.format(fname)
                +'and will consider aperture ranges listed there. If this metadata file is not a part\n'
                +'of the run you intended, please provide a more precise folder for the metadata file.')

    fname = fname[:-4] # Strip off the .dat ending

    s4_meta = me.loadevent(fname)

    return s4_meta

def load_general_s4_meta_info(meta, s4_meta):

    # Need to remove the topdir from the outputdir
    s4_outputdir = s4_meta.outputdir[len(s4_meta.topdir):]
    if s4_outputdir[0]=='/':
        s4_outputdir = s4_outputdir[1:]
    if s4_outputdir[-1]!='/':
        s4_outputdir += '/'
    s4_allapers = s4_meta.allapers

    # Overwrite the temporary meta object made above to be able to find s4_meta
    meta = s4_meta

    # Load Eureka! control file and store values in the S4 metadata object
    ecffile = 'S5_' + meta.eventlabel + '.ecf'
    ecf     = rd.read_ecf(ecffile)
    rd.store_ecf(meta, ecf)

    # Overwrite the inputdir with the exact output directory from S4
    meta.inputdir = s4_outputdir
    meta.old_datetime = s4_meta.datetime # Capture the date that the
    meta.datetime = None # Reset the datetime in case we're running this on a different day
    meta.inputdir_raw = meta.inputdir
    meta.outputdir_raw = meta.outputdir

    meta.s4_allapers = s4_allapers

    return meta

def load_specific_s4_meta_info(meta, run_i, spec_hw_val, bg_hw_val):
    # Do some folder swapping to be able to reuse this function to find the correct S4 outputs
    tempfolder = meta.outputdir_raw
    meta.outputdir_raw = '/'.join(meta.inputdir_raw.split('/')[:-2])
    meta.inputdir = util.pathdirectory(meta, 'S4', meta.runs_s4[run_i], old_datetime=meta.old_datetime, ap=spec_hw_val, bg=bg_hw_val)
    meta.outputdir_raw = tempfolder

    # Read in the correct S4 metadata for this aperture pair
    tempfolder = meta.inputdir
    meta.inputdir = meta.inputdir[len(meta.topdir):]
    new_meta = read_s4_meta(meta)
    meta.inputdir = tempfolder

    # Load S5 Eureka! control file and store values in the S4 metadata object
    ecffile = 'S5_' + meta.eventlabel + '.ecf'
    ecf     = rd.read_ecf(ecffile)
    rd.store_ecf(new_meta, ecf)

    # Save correctly identified folders from earlier
    new_meta.inputdir = meta.inputdir
    new_meta.outputdir = meta.outputdir
    new_meta.inputdir_raw = meta.inputdir_raw
    new_meta.outputdir_raw = meta.outputdir_raw

    new_meta.runs_s5 = meta.runs_s5
    new_meta.datetime = meta.datetime

    return new_meta
