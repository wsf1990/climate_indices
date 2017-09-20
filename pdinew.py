import calendar
import logging
import math
import numba
import numpy as np
import pandas as pd
import utils
import warnings

# set up a basic, global logger
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s',
                    datefmt='%Y-%m-%d  %H:%M:%S')
logger = logging.getLogger(__name__)

# set numpy's print options so when array values are printed we can control the precision
np.set_printoptions(formatter={'float': lambda x: "{0:.2f}".format(x)})

#-----------------------------------------------------------------------------------------------------------------------
def pdsi_from_climatology(precip_timeseries,
                          temp_timeseries,
                          awc,
                          latitude,
                          B,
                          H,
                          data_begin_year,
                          data_end_year,
                          calibration_begin_year,
                          calibration_end_year,
                          expected_pdsi_for_debug):
    
    # calculate the negative tangent of the latitude which is used as an argument to the water balance function
    neg_tan_lat = -1 * math.tan(math.radians(latitude))

    # compute water balance values using the function translated from the Fortran pdinew.f
    #NOTE keep this code in place in order to compute the PET used later, since the two have 
    # different PET algorithms and we want to compare PDSI using the same PET inputs
    #FIXME clarify the difference between SP and PRO (spdat and prodat)
    pdat, spdat, pedat, pldat, prdat, rdat, tldat, etdat, rodat, prodat, tdat, sssdat, ssudat = \
        _water_balance(temp_timeseries, precip_timeseries, awc, neg_tan_lat, B, H)
                 
    #NOTE we need to compute CAFEC coefficients for use later/below
    # compute PDSI etc. using translated functions from pdinew.f Fortran code
    alpha, beta, delta, gamma, t_ratio = _cafec_coefficients(precip_timeseries,
                                                             pedat,
                                                             etdat,
                                                             prdat,
                                                             rdat,
                                                             rodat,
                                                             prodat,
                                                             tldat,
                                                             pldat,
                                                             spdat,
                                                             data_begin_year,
                                                             calibration_begin_year,
                                                             calibration_end_year)
     
    # compute the weighting factor (climatic characteristic) using the version translated from pdinew.f
    K = _climatic_characteristic(alpha,
                                 beta,
                                 gamma,
                                 delta,
                                 pdat,
                                 pedat,
                                 prdat,
                                 spdat,
                                 pldat,
                                 t_ratio,
                                 data_begin_year,
                                 calibration_begin_year,
                                 calibration_end_year)

    # compute the Z-index
    #TODO eliminate the P-hat once development/debugging is complete, since it's really an intermediate/internal value
    Z, P_hat = _zindex(alpha, 
                       beta, 
                       gamma, 
                       delta, 
                       precip_timeseries, 
                       pedat, 
                       prdat, 
                       spdat, 
                       pldat, 
                       K)

    # compute the final Palmers
    #TODO/FIXME eliminate the expected PDSI argument once development/debugging is complete
    PDSI, PHDI, PMDI = _pdsi(precip_timeseries,
                             Z,
                             K, 
                             data_begin_year, 
                             data_end_year,
                             expected_pdsi_for_debug)
    
    return PDSI, PHDI, PMDI, Z

#-----------------------------------------------------------------------------------------------------------------------
#@numba.jit
def _cafec_coefficients(P,
                        PET,
                        ET,
                        PR,
                        R,
                        RO,
                        PRO,
                        L,
                        PL,
                        SP,
                        data_start_year,
                        calibration_start_year,
                        calibration_end_year):
    '''
    This function calculates CAFEC coefficients used for computing Palmer's Z index using inputs from 
    the water balance function. Translated from Fortran pdinew.f
    
    :param P: 1-D numpy.ndarray of monthly precipitation observations, in inches, the number of array elements 
              (array size) should be a multiple of 12 (representing an ordinal number of full years)
    :param PET: 1-D numpy.ndarray of monthly potential evapotranspiration values, in inches, the number of array elements 
                (array size) should be a multiple of 12 (representing an ordinal number of full years)
    :param ET: 1-D numpy.ndarray of monthly evapotranspiration values, in inches, the number of array elements 
               (array size) should be a multiple of 12 (representing an ordinal number of full years)
    :param PR: 1-D numpy.ndarray of monthly potential recharge values, in inches, the number of array elements 
               (array size) should be a multiple of 12 (representing an ordinal number of full years)
    :param R: 1-D numpy.ndarray of monthly recharge values, in inches, the number of array elements 
              (array size) should be a multiple of 12 (representing an ordinal number of full years)
    :param RO: 1-D numpy.ndarray of monthly runoff values, in inches, the number of array elements 
               (array size) should be a multiple of 12 (representing an ordinal number of full years)
    :param PRO: 1-D numpy.ndarray of monthly potential runoff values, in inches, the number of array elements 
                (array size) should be a multiple of 12 (representing an ordinal number of full years)
    :param L: 1-D numpy.ndarray of monthly loss values, in inches, the number of array elements 
              (array size) should be a multiple of 12 (representing an ordinal number of full years)
    :param PL: 1-D numpy.ndarray of monthly potential loss values, in inches, the number of array elements 
              (array size) should be a multiple of 12 (representing an ordinal number of full years)
    :param SP: 1-D numpy.ndarray of monthly SP values, in inches, the number of array elements 
               (array size) should be a multiple of 12 (representing an ordinal number of full years)
    :param data_start_year: initial year of the input arrays, i.e. the first element of each of the input arrays 
                            is assumed to correspond to January of this initial year
    :param calibration_start_year: initial year of the calibration period, should be greater than or equal to the data_start_year
    :param calibration_end_year: final year of the calibration period
    :return 1-D numpy.ndarray of CAFEC coefficient values, alpha, beta, delta, gamma, and the T ratio,
            with shape of these arrays == (12,), corresponding to calendar months (12 elements)
    :rtype: numpy.ndarray of floats
    '''
    
    # the potential (PET, ET, PR, PL) and actual (R, RO, S, L, P) water balance arrays are reshaped as 2-D arrays  
    # (matrices) such that the rows of each matrix represent years and the columns represent calendar months
    PET = utils.reshape_to_years_months(PET)
    ET = utils.reshape_to_years_months(ET)
    PR = utils.reshape_to_years_months(PR)
    PL = utils.reshape_to_years_months(PL)
    R = utils.reshape_to_years_months(R)
    RO = utils.reshape_to_years_months(RO)
    PRO = utils.reshape_to_years_months(PRO)
    L = utils.reshape_to_years_months(L)
    P = utils.reshape_to_years_months(P)
    SP = utils.reshape_to_years_months(SP)
        
    # ALPHA, BETA, GAMMA, DELTA CALCULATIONS
    # A calibration period is used to calculate alpha, beta, gamma, and 
    # and delta, four coefficients dependent on the climate of the area being
    # examined. The NCDC and CPC use the calibration period January 1931
    # through December 1990 (cf. Karl, 1986; Journal of Climate and Applied 
    # Meteorology, Vol. 25, No. 1, January 1986).
    
    #!!!!!!!!!!!!!
    # TODO make sure calibration years range is valid, i.e. within actual data years range 
    
    # determine the array (year axis) indices for the calibration period
    total_data_years = int(P.shape[0] / 12)
    data_end_year = data_start_year + total_data_years - 1
    total_calibration_years = calibration_end_year - calibration_start_year + 1
    calibration_start_year_index = calibration_start_year - data_start_year
    calibration_end_year_index = calibration_end_year - data_start_year 
    
    # get calibration period arrays
    if (calibration_start_year > data_start_year) or (calibration_end_year < data_end_year):
        P_calibration = P[calibration_start_year_index:calibration_end_year_index + 1]
        ET_calibration = ET[calibration_start_year_index:calibration_end_year_index + 1]
        PET_calibration = PET[calibration_start_year_index:calibration_end_year_index + 1]
        R_calibration = R[calibration_start_year_index:calibration_end_year_index + 1]
        PR_calibration = PR[calibration_start_year_index:calibration_end_year_index + 1]
        L_calibration = L[calibration_start_year_index:calibration_end_year_index + 1]
        PL_calibration = PL[calibration_start_year_index:calibration_end_year_index + 1]
        RO_calibration = RO[calibration_start_year_index:calibration_end_year_index + 1]
        PRO_calibration = PRO[calibration_start_year_index:calibration_end_year_index + 1]
        SP_calibration = SP[calibration_start_year_index:calibration_end_year_index + 1]
    else:
        P_calibration = P
        ET_calibration = ET
        PET_calibration = PET
        R_calibration = R
        PR_calibration = PR
        L_calibration = L
        PL_calibration = PL
        RO_calibration = RO
        PRO_calibration = PRO
        SP_calibration = SP

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        
#         # get averages for each calendar month (compute means over the year axis, giving an average for each calendar month over all years)
#         P_bar = np.nanmean(P_calibration, axis=0)
#         ET_bar = np.nanmean(ET_calibration, axis=0)
#         PET_bar = np.nanmean(PET_calibration, axis=0)
#         R_bar = np.nanmean(R_calibration, axis=0)
#         PR_bar = np.nanmean(PR_calibration, axis=0)
#         L_bar = np.nanmean(L_calibration, axis=0)
#         PL_bar = np.nanmean(PL_calibration, axis=0)
#         RO_bar = np.nanmean(RO_calibration, axis=0)
#         PRO_bar = np.nanmean(PRO_calibration, axis=0)
            
        # get sums for each calendar month (compute sums over the year axis, giving a sum for each calendar month over all years)
        P_sum = np.nansum(P_calibration, axis=0)
        ET_sum = np.nansum(ET_calibration, axis=0)
        PET_sum = np.nansum(PET_calibration, axis=0)
        R_sum = np.nansum(R_calibration, axis=0)
        PR_sum = np.nansum(PR_calibration, axis=0)
        L_sum = np.nansum(L_calibration, axis=0)
        PL_sum = np.nansum(PL_calibration, axis=0)
        RO_sum = np.nansum(RO_calibration, axis=0)
        SP_sum = np.nansum(SP_calibration, axis=0)
            
        # (calendar) monthly CAFEC coefficients
        alpha = np.empty((12,))
        beta = np.empty((12,))
        gamma = np.empty((12,))
        delta = np.empty((12,))
        t_ratio = np.empty((12,))
    
        # compute the alpha, beta, gamma, and delta coefficients for each calendar month
        for i in range(12):
            
            #     ALPHA CALCULATION 
            #     ----------------- 
            if PET_sum[i] != 0.0:   
                alpha[i] = ET_sum[i] / PET_sum[i]  
            else:  
                if ET_sum[i] == 0.0:  
                    alpha[i] = 1.0  
                else:  
                    alpha[i] = 0.0  
            
            #   
            #     BETA CALCULATION  
            #     ----------------  
            if PR_sum[i] != 0.0:  
                beta[i] = R_sum[i] / PR_sum[i] 
            else:  
                if R_sum[i] == 0.0:   
                    beta[i] = 1.0  
                else:  
                    beta[i] = 0.0  

            #   
            #     GAMMA CALCULATION 
            #     ----------------- 
            if SP_sum[i] != 0.0:  
                gamma[i] = RO_sum[i] / SP_sum[i]  
            else:  
                if RO_sum[i] == 0.0:  
                    gamma[i] = 1.   
                else:  
                    gamma[i] = 0.0  

            #   
            #     DELTA CALCULATION 
            #     ----------------- 
            if PL_sum[i] != 0.0:  
                delta[i] = L_sum[i] / PL_sum[i]  
            else:
                delta[i] = 0.0  

            # 'T' ratio of average moisture demand to the average moisture supply in the month
            t_ratio[i] = (PET_sum[i] + R_sum[i] + RO_sum[i]) / (P_sum[i] + L_sum[i])

    return alpha, beta, delta, gamma, t_ratio

#-----------------------------------------------------------------------------------------------------------------------
@numba.jit
def _climatic_characteristic(alpha,
                             beta,
                             gamma,
                             delta,
                             Pdat,
                             PEdat,
                             PRdat,
                             SPdat,
                             PLdat,
                             t_ratio,
                             begin_year,
                             calibration_begin_year,
                             calibration_end_year):
    
    SABSD = np.zeros((12,))
    
    number_calibration_years = calibration_end_year - calibration_begin_year + 1
    
    # loop over the calibration years, get the sum of the absolute values of the moisture departure SABSD for each month
    for j in range(calibration_begin_year - begin_year, calibration_end_year - begin_year + 1):
        for m in range(12):
            
            #-----------------------------------------------------------------------
            #     REREAD MONTHLY PARAMETERS FOR CALCULATION OF 
            #     THE 'K' MONTHLY WEIGHTING FACTORS USED IN Z-INDEX CALCULATION 
            #-----------------------------------------------------------------------
            PHAT = (alpha[m] * PEdat[j, m]) + (beta[m] * PRdat[j, m]) + (gamma[m] * SPdat[j, m]) - (delta[m] * PLdat[j, m])  
            D = Pdat[j, m] - PHAT   
            SABSD[m] = SABSD[m] + abs(D) 

    # get the first approximation of K (weighting factor)
    SWTD = 0.0
    AKHAT = np.empty((12,))
    for m in range(12):
        DBAR = SABSD[m] / number_calibration_years 
        AKHAT[m] = 1.5 * math.log10((t_ratio[m] + 2.8) / DBAR) + 0.5
        SWTD = SWTD + (DBAR * AKHAT[m])  

    AK = np.empty((12,))
    for m in range(12):
        AK[m] = 17.67 * AKHAT[m] / SWTD 
 
    return AK

#-----------------------------------------------------------------------------------------------------------------------
@numba.jit
def _water_balance(T,
                   P,
                   AWC,
                   TLA,
                   B, 
                   H,
                   begin_year=1895):

    '''
    Computes a water balance accounting for monthly time series. Translated from the Fortran code pdinew.f
    
    :param T: monthly average temperature values, starting in January of the initial year 
    :param P: monthly total precipitation values, starting in January of the initial year 
    :param AWC: available water capacity, below (not including) the top inch
    :param B: read from soil constants file 
    :param H: read from soil constants file
    :param begin_year: initial year of the dataset  
    :param TLA: negative tangent of the latitude
    :return: P, SP, PE, PL, PR, R, L, ET, RO, T, SSs, SSu
    :rtype: numpy arrays with shape (total_years, 12)
    '''
    #TODO document the differences between PRO and SP, as the SP return value (spdat) is being used as PRO (potential
    # runoff) in later parts of the code, let's verify that this is intended, or perhaps spdat and prodat are 
    # synonymous/duplicates and one of these can be eliminated, suspicion is that the PRO variable in pdinew.f
    # refers to a probability value, so the variable for potential runoff is named SP instead (?)
    
    # find the number of years from the input array, assume shape (months)
    
    # reshape the precipitation array from 1-D (assumed to be total months) to (years, 12) with the second  
    # dimension being calendar months, and the final/missing monthly values of the final year padded with NaNs
    T = utils.reshape_to_years_months(T)
    P = utils.reshape_to_years_months(P)
    total_years = P.shape[0]
    
    WCTOP = 1.0
    SS  = WCTOP
    SU = AWC
    WCTOT = AWC + WCTOP

    #TODO document this, where do these come from?
    PHI = np.array([-0.3865982, -0.2316132, -0.0378180, 0.1715539, 0.3458803, 0.4308320, \
                     0.3916645, 0.2452467, 0.0535511, -0.15583436, -0.3340551, -0.4310691])
    
    # initialize the data arrays with NaNs    
    pdat = np.full((total_years, 12), np.NaN)
    spdat = np.full((total_years, 12), np.NaN)
    pedat = np.full((total_years, 12), np.NaN)
    pldat = np.full((total_years, 12), np.NaN)
    prdat = np.full((total_years, 12), np.NaN)
    rdat = np.full((total_years, 12), np.NaN)
    tldat = np.full((total_years, 12), np.NaN)
    etdat = np.full((total_years, 12), np.NaN)
    rodat = np.full((total_years, 12), np.NaN)
    prodat = np.full((total_years, 12), np.NaN)
    tdat = np.full((total_years, 12), np.NaN)
    sssdat = np.full((total_years, 12), np.NaN)
    ssudat = np.full((total_years, 12), np.NaN)

    # loop on years and months
    end_year = begin_year + total_years
    years_range = range(begin_year, end_year)
    for year_index, year in enumerate(years_range):
    
        for month_index in range(12):
    
            temperature = T[year_index, month_index]
            precipitation = P[year_index, month_index]
            
            #-----------------------------------------------------------------------
            #     HERE START THE WATER BALANCE CALCULATIONS
            #-----------------------------------------------------------------------
            SP = SS + SU
            PR = AWC + WCTOP - SP

            # PRO is the potential runoff. According to Alley (1984),
            # PRO = AWC - PR = Ss + Su; with Ss and Su referring to those values at
            # the beginning of the month: Ss0 and Su0.
            PRO = SP

            #-----------------------------------------------------------------------
            #     1 - CALCULATE PE (POTENTIAL EVAPOTRANSPIRATION)   
            #-----------------------------------------------------------------------
            if temperature <= 32.0:
                PE = 0.0
            else:  
                DUM = PHI[month_index] * TLA 
                DK = math.atan(math.sqrt(1.0 - (DUM * DUM)) / DUM)   
                if DK < 0.0:
                    DK = 3.141593 + DK  
                DK = (DK + 0.0157) / 1.57  
                if temperature >= 80.0:
                    PE = (math.sin((temperature / 57.3) - 0.166) - 0.76) * DK
                else:  
                    DUM = math.log(temperature - 32.0)
                    PE = math.exp(-3.863233 + (B * 1.715598) - (B * math.log(H)) + (B * DUM)) * DK 
        
            #-----------------------------------------------------------------------
            #     CONVERT DAILY TO MONTHLY  
            #-----------------------------------------------------------------------
            PE = PE * calendar.monthrange(year, month_index + 1)[1]

            #-----------------------------------------------------------------------
            #     2 - PL  POTENTIAL LOSS
            #-----------------------------------------------------------------------
            if SS >= PE:
                PL  = PE  
            else:  
                PL = ((PE - SS) * SU) / (AWC + WCTOP) + SS   
                PL = min(PL, SP)   
        
            #-----------------------------------------------------------------------
            #     3 - CALCULATE RECHARGE, RUNOFF, RESIDUAL MOISTURE, LOSS TO BOTH   
            #         SURFACE AND UNDER LAYERS, DEPENDING ON STARTING MOISTURE  
            #         CONTENT AND VALUES OF PRECIPITATION AND EVAPORATION.  
            #-----------------------------------------------------------------------
            if precipitation >= PE:
                #     ----------------- PRECIP EXCEEDS POTENTIAL EVAPORATION
                ET = PE   
                TL = 0.0  
                if (precipitation - PE) > (WCTOP - SS):
                    #         ------------------------------ EXCESS PRECIP RECHARGES
                    #                                        UNDER LAYER AS WELL AS UPPER   
                    RS = WCTOP - SS  
                    SSS = WCTOP  
                    if (precipitation - PE - RS) < (AWC - SU):
                    #             ---------------------------------- BOTH LAYERS CAN TAKE   
                    #                                                THE ENTIRE EXCESS  
                        RU = precipitation - PE - RS  
                        RO = 0.0  
                    else:  
                        #             ---------------------------------- SOME RUNOFF OCCURS 
                        RU = AWC - SU   
                        RO = precipitation - PE - RS - RU 

                    SSU = SU + RU 
                    R   = RS + RU 
                else:  
                    #         ------------------------------ ONLY TOP LAYER RECHARGED   
                    R  = precipitation - PE  
                    SSS = SS + precipitation - PE 
                    SSU = SU  
                    RO  = 0.0 

            else:
                #     ----------------- EVAPORATION EXCEEDS PRECIPITATION   
                R  = 0.0  
                if SS >= (PE - precipitation):
                #         ----------------------- EVAP FROM SURFACE LAYER ONLY  
                    SL  = PE - precipitation  
                    SSS = SS - SL 
                    UL  = 0.0 
                    SSU = SU  
                else:
                    #         ----------------------- EVAP FROM BOTH LAYERS 
                    SL  = SS  
                    SSS = 0.0 
                    UL  = (PE - precipitation - SL) * SU / (WCTOT)  
                    UL  = min(UL, SU)
                    SSU = SU - UL 

                TL  = SL + UL 
                RO  = 0.0 
                ET  = precipitation  + SL + UL

            # set the climatology and water balance data array values for this year/month time step
            pdat[year_index, month_index] = precipitation
            spdat[year_index, month_index] = SP
            pedat[year_index, month_index] = PE
            pldat[year_index, month_index] = PL
            prdat[year_index, month_index] = PR
            rdat[year_index, month_index] = R
            tldat[year_index, month_index] = TL
            etdat[year_index, month_index] = ET
            rodat[year_index, month_index] = RO
            prodat[year_index, month_index] = PRO
            tdat[year_index, month_index] = temperature
            sssdat[year_index, month_index] = SSS
            ssudat[year_index, month_index] = SSU
      
            # reset the upper and lower soil moisture values
            SS = SSS
            SU = SSU

    return pdat, spdat, pedat, pldat, prdat, rdat, tldat, etdat, rodat, prodat, tdat, sssdat, ssudat

#-----------------------------------------------------------------------------------------------------------------------
def _zindex(alpha,
            beta,
            gamma,
            delta,
            P,
            PE,
            PR,
            PRO,
            PL,
            K):
    '''
    Compute the Z-Index and CAFEC precipitation value for a single monthly time step.
    
    :param alpha: array of the monthly "alpha" CAFEC coefficients, 1-D with 12 elements, one per calendar month 
    :param beta: array of the monthly "beta" CAFEC coefficients, 1-D with 12 elements, one per calendar month 
    :param gamma: array of the monthly "delta" CAFEC coefficients, 1-D with 12 elements, one per calendar month 
    :param delta: array of the monthly "gamma" CAFEC coefficients, 1-D with 12 elements, one per calendar month 
    :param P: array of monthly precipitation values, 1-D with total size matching the corresponding 2-D arrays 
    :param PE: array of monthly potential evapotranspiration values, 2-D with shape (# of years, 12) 
    :param PR: array of monthly potential recharge values, 2-D with shape (# of years, 12) 
    :param PRO: array of monthly potential runoff values, 2-D with shape (# of years, 12) 
    :param PL: array of monthly potential loss values, 2-D with shape (# of years, 12) 
    :param K: array of the monthly climatic characteristic, 1-D with 12 elements, one per calendar month 
    :return Z-Index and CAFEC precipitation (P-hat)
    :rtype: two floats
    '''
    
    # reshape the precipitation array, expected to have 1-D shape, to 2-D shape (years, 12)
    P = utils.reshape_to_years_months(P)
    
    # allocate the Z-Index and P-hat arrays we'll build and return
    Z = np.full(P.shape, np.NaN)
    P_hat = np.full(P.shape, np.NaN)
    
    # loop over all years and months of the time series
    for j in range(P.shape[0]):    
                
        for m in range(12):

            # compute the CAFEC precipitation (P-hat)
            ET_hat = alpha[m] * PE[j, m]             # eq. 10, Palmer 1965
            R_hat = beta[m] * PR[j, m]               # eq. 11, Palmer 1965
            RO_hat = gamma[m] * PRO[j, m]            # eq. 12, Palmer 1965
            L_hat = delta[m] * PL[j, m]              # eq. 13, Palmer 1965
            P_hat = ET_hat + R_hat + RO_hat - L_hat  # eq. 14, Palmer 1965
            
            # moisture departure
            d = P[j, m] - P_hat      # eq. 15, Palmer 1965 
            
            # Z-Index
            Z[j, m] = K[m] * d    # eq. 19, Palmer 1965 
            
    return Z, P_hat

#-----------------------------------------------------------------------------------------------------------------------
def _pdsi(P,
          Z,
          K,
          nbegyr,#=1895,
          nendyr,#=2017
          expected_pdsi):
    '''
    :param P: 1-D array of precipitation values
    :param Z: 2-D array of Z-Index values, corresponding in total size to P
    :param K: 1-D array of climatic characteristic values, one per calendar month (12 total)
    :param nbegyr: initial year of input datasets (P, Z)
    :param nbegyr: final year (inclusive) of input datasets (P, Z)
    :param expected_pdsi: for DEBUGGING/DEBUG only -- REMOVE 
    '''
    # reshape the expected PDSI to match with others (in case of mismatch)
    expected_pdsi = np.reshape(expected_pdsi, P.shape)
    
    # reshape the precipitation array to (total # of years, 12)
    P = utils.reshape_to_years_months(P)
    
    # intermediate values used in computations below 
    PV = 0.0
    V = 0.0
    
    # provisional severity index values computed for each month/timestep, typically carried forward for use
    # in the next timestep, since current month X values are computed using the previous month's X values 
    X1 = 0.0   # index appropriate to a wet spell that is becoming established, as well as the percent chance that a wet spell has begun 
    X2 = 0.0   # index appropriate to a drought that is becoming established, as well as the percent chance that a drought has begun
    X3 = 0.0   # index appropriate to a wet spell or drought that has already been established
    
    # total number of backtrack months, i.e. when backtracking we'll back fill this many months
    K8 = 0
    
    # percentage probability that an established weather spell (wet or dry) has ended (Pe in Palmer 1965, eq. 30)
    prob_ended = 0.0   ##NOTE this was PRO in the original Fortran pdinew.f, now the variable name PRO is used for potential runoff

    # create a DataFrame to use as a container for the arrays and values we'll use throughout the computation loop below
    array0 = np.reshape(P, (P.size, 1))
    df = pd.DataFrame(data=array0, 
                      index=range(0, array0.size), 
                      columns=['P'])

    # create a list of coluumn names that match to the intermediate work arrays
    column_names = ['PPR', 'PDSI', 'PHDI', 'WPLM', 'SX', 'SX1', 'SX2', 'SX3', 'X', 'PX1', 'PX2', 'PX3']
    for column_name in column_names:
        
        # get the array corresponding to the current column
        column_array = np.full(P.shape, np.NaN).flatten()
        
        # add the column to the DataFrame (as a Series)
        df[column_name] = pd.Series(column_array)

    # add the year and month index columns used to keep track of which year/month index to use for the start of backtracking
    column_names = ['index_j', 'index_m']
    for column_name in column_names:
        
        # get the array corresponding to the current column
        column_array = np.full(P.shape, 0, dtype=int).flatten()
        
        # add the column to the DataFrame (as a Series)
        df[column_name] = pd.Series(column_array)

    # add the expected PDSI values so we can compare against these as we're debugging
    df['expected_pdsi'] = pd.Series(expected_pdsi)
    
    # add the Z-Index array into the DataFrame as a Series column 
    df['Z'] = pd.Series(Z.flatten())
    
    # loop over all years and months of the time series
    for j in range(P.shape[0]):    
                
        for m in range(12):

            i = (j * 12) + m

            # DEBUGGING ONLY -- REMOVE
            print('i: {0}'.format(i))
            if i == 58:
                print('debug breakpoint')
            
            # these indices keep track of the latest year (j) and month (m) indices corresponding 
            # to the current backtracking index (K8), where K8 > 0 indicates backtracking is required
            df.index_j[K8] = j
            df.index_m[K8] = m

            # original comments from pdinew.f, left in place to facilitate development
            #TODO/FIXME update/replace
            #-----------------------------------------------------------------------
            #     LOOP FROM 160 TO 230 REREADS data FOR CALCULATION OF   
            #     THE Z-INDEX (MOISTURE ANOMALY) AND PDSI (VARIABLE X). 
            #     THE FINAL OUTPUTS ARE THE VARIABLES PX3, X, AND Z  WRITTEN
            #     TO FILE 11.   
            #-----------------------------------------------------------------------
             
            if prob_ended == 100.0 or prob_ended == 0.0:  
            #     ------------------------------------ NO ABATEMENT UNDERWAY
            #                                          WET OR DROUGHT WILL END IF   
            #                                             -0.5 =< X3 =< 0.5   
                
                # "near normal" is defined as the range [-0.5 ... 0.5], Palmer 1965 p. 29
                if abs(X3) <= 0.5:
                #         ---------------------------------- END OF DROUGHT OR WET  
                    PV = 0.0 
                    df.PPR[i] = 0.0 
                    df.PX3[i] = 0.0 
                    #             ------------ BUT CHECK FOR NEW WET OR DROUGHT START FIRST
                    # GOTO 200 in pdinew.f
                    df, X1, X2, X3, V, prob_ended, K8 = _compute_X(df, X1, X2, j, m, K8, nendyr, nbegyr, PV)
                     
                elif X3 > 0.5:   
                    #         ----------------------- WE ARE IN A WET SPELL 
                    if df.Z[i] >= 0.15:   
                        #              ------------------ THE WET SPELL INTENSIFIES 
                        #GO TO 210 in pdinew.f
                        df, X1, X2, X3, V, prob_ended, K8 = _between_0s(df, K8, X3, j, m, nendyr, nbegyr)
                        
                        
                    else:
                        #             ------------------ THE WET STARTS TO ABATE (AND MAY END)  
                        #GO TO 170 in pdinew.f
                        df, X1, X2, X3, V, prob_ended, K8 = _wet_spell_abatement(df, V, K8, prob_ended, j, m, nendyr, nbegyr, X1, X2, X3)

                elif X3 < -0.5:  
                    #         ------------------------- WE ARE IN A DROUGHT
                    
                    # in order to start to pull out of a drought the Z-Index for the month needs to be >= -0.15 (Palmer 1965, eq. 29)
                    if df.Z[i] < -0.15:  #NOTE pdinew.f uses <= here, rather than <: "IF (Z(j,m).LE.-.15) THEN..."
                        #              -------------------- THE DROUGHT INTENSIFIES 
                        #GO TO 210
                        df, X1, X2, X3, V, prob_ended, K8 = _between_0s(df, K8, X3, j, m, nendyr, nbegyr)

                    else:
                        # Palmer 1965, p. 29: "any value of Z >= -0.15 will tend to end a drought"
                        #             ------------------ THE DROUGHT STARTS TO ABATE (AND MAY END)  
                        #GO TO 180
                        df, X1, X2, X3, V, prob_ended, K8 = _dry_spell_abatement(df, K8, j, m, nendyr, nbegyr, V, X1, X2, X3, prob_ended)

            else:
                #     ------------------------------------------ABATEMENT IS UNDERWAY   
                if X3 > 0.0:
                    
                    #         ----------------------- WE ARE IN A WET SPELL 
                    #GO TO 170 in pdinew.f
                    df, X1, X2, X3, V, prob_ended, K8 = _wet_spell_abatement(df, V, K8, prob_ended, j, m, nendyr, nbegyr, X1, X2, X3)
                
                else:  # if X3 <= 0.0:
                    
                    #         ----------------------- WE ARE IN A DROUGHT   
                    #GO TO 180
                    df, X1, X2, X3, V, prob_ended, K8 = _dry_spell_abatement(df, K8, j, m, nendyr, nbegyr, V, X1, X2, X3, prob_ended)

    # clear out the remaining values from the backtracking array, if any are left, assigning into the indices arrays
    for x in range(0, K8):

        # turn the 2-D backtracking (years and months) indices into an 1-D series index
        ix = (df.index_j[x] * 12) + df.index_m[x]
        
        df.PDSI[ix] = df.X[ix] 
        df.PHDI[ix] = df.PX3[ix]
        
        if df.PX3[ix] == 0.0:
        
            df.PHDI[ix] = df.X[ix]
        
        df.WPLM[ix] = _case(df.PPR[ix], df.PX1[ix], df.PX2[ix], df.PX3[ix]) 
      
    # return the Palmer severity index values as 1-D arrays
    return df.PDSI.values, df.PHDI.values, df.WPLM.values

#-----------------------------------------------------------------------------------------------------------------------
# from label 190 in pdinew.f
def _get_PPR_PX3(df,
                 prob_ended,
                 Ze,
                 V,
                 PV,
                 ix,
                 X3):
    
    #-----------------------------------------------------------------------
    #     PROB(END) = 100 * (V/Q)  WHERE:   
    #             V = SUM OF MOISTURE EXCESS OR DEFICIT (UD OR UW)  
    #                 DURING CURRENT ABATEMENT PERIOD   
    #             Q = TOTAL MOISTURE ANOMALY REQUIRED TO END THE
    #                 CURRENT DROUGHT OR WET SPELL  
    #-----------------------------------------------------------------------
    if prob_ended == 100.0: 
        #     --------------------- DROUGHT OR WET CONTINUES, CALCULATE 
        #                           PROB(END) - VARIABLE Ze 
        Q = Ze
    else:  
        Q = Ze + V

    # percentage probability that an established drought or wet spell has ended
    df.PPR[ix] = (PV / Q) * 100.0   # eq. 30 Palmer 1965
    if df.PPR[ix] >= 100.0:
         
          df.PPR[ix] = 100.0
          df.PX3[ix] = 0.0  
    else:
          # eq. 25, Palmer (1965); eq. 4, Wells et al (2003) 
          df.PX3[ix] = (0.897 * X3) + (df.Z[ix] / 3.0)

    return df

#-----------------------------------------------------------------------------------------------------------------------
# from label 170 in pdinew.f
def _wet_spell_abatement(df,
                         V, 
                         K8, 
                         prob_ended,
                         j, 
                         m, 
                         nendyr, 
                         nbegyr,
                         X1, 
                         X2, 
                         X3):
    
    # combine the years (j) and months (m) indices to series index i
    i = (j * 12) + m
        
    #-----------------------------------------------------------------------
    #      WET SPELL ABATEMENT IS POSSIBLE  
    #-----------------------------------------------------------------------
    Ud = df.Z[i] - 0.15  
    PV = Ud + min(V, 0.0) 
    if PV >= 0.0:

        # GO TO label 210 in pdinew.f
        df, X1, X2, X3, V, prob_ended, K8 = _between_0s(df, K8, X3, j, m, nendyr, nbegyr)

    else:
        #     ---------------------- DURING A WET SPELL, PV => 0 IMPLIES
        #                            PROB(END) HAS RETURNED TO 0
        Ze = -2.691 * X3 + 1.5

        # compute the PPR and PX3 values    
        df = _get_PPR_PX3(df, prob_ended, Ze, V, PV, i, X3)
        
    # continue at label 200 in pdinew.f
    # recompute the X values and other intermediates
    return _compute_X(df, X1, X2, j, m, K8, nendyr, nbegyr, PV)

#-----------------------------------------------------------------------------------------------------------------------
# from label 180 in pdinew.f
def _dry_spell_abatement(df,
                         K8,
                         j, 
                         m, 
                         nendyr, 
                         nbegyr, 
#                          PV,
                         V,   # accumulated effective wetness
                         X1, 
                         X2,
                         X3,
                         prob_ended):
    '''
    
    '''
    
    # combine the two-dimensional array indices (years (j) and months (m)) into a one-dimensional series index (i)
    i = (j * 12) + m
        
    #-----------------------------------------------------------------------
    #      DROUGHT ABATEMENT IS POSSIBLE
    #-----------------------------------------------------------------------
    
    # the "effective wetness" of the current month
    Uw = df.Z[i] + 0.15   # Palmer 1965, eq. 29
    
    # add this month's effective wetness to the previously accumulated effective wetness (if it was positive),
    # this value will eventually be used as the numerator in the equation for calculating percentage probability
    # that an established weather spell has ended (eq. 30, Palmer 1965)
    PV = Uw + max(V, 0.0)
    
    # if this month's cumulative effective wetness value isn't positive then there is 
    # insufficient moisture this month to end or even abate the established dry spell
    if PV <= 0:
        #     ---------------------- DURING A DROUGHT, PV =< 0 IMPLIES  
        #                            PROB(END) HAS RETURNED TO 0        
        # GOTO 210 in pdinew.f
        df, X1, X2, X3, V, prob_ended, K8 = _between_0s(df, K8, X3, j, m, nendyr, nbegyr)
            
    else:
        # abatement is underway
        
        # Calculate the Z value which corresponds to an amount of moisture that is sufficient to end 
        # the currently established drought in a single month. Once this is known then we can compare 
        # against the actual Z value, to see if we've pulled out of the established drought or not 
        Ze = -2.691 * X3 - 1.5   # eq. 28, Palmer 1965
        
        # compute the percentage probability that the established spell has ended (PPR),
        # and the severity index for the established spell (PX3)
        df = _get_PPR_PX3(df, prob_ended, Ze, V, PV, i, X3)
        
    # continue at label 200 in pdinew.f
    # recompute the X values and other intermediates
    return _compute_X(df, X1, X2, j, m, K8, nendyr, nbegyr, PV)

#-----------------------------------------------------------------------------------------------------------------------
# label 200 in pdinew.f
def _compute_X(df,
               X1,
               X2,
               j,
               m,
               K8,
               nendyr, 
               nbegyr, 
               PV):
    
    # the values within the DataFrame are in a series, so get the single index value assuming j is years and m is months
    i = (j * 12) + m

    #-----------------------------------------------------------------------
    #     CONTINUE X1 AND X2 CALCULATIONS.  
    #     IF EITHER INDICATES THE START OF A NEW WET OR DROUGHT,
    #     AND IF THE LAST WET OR DROUGHT HAS ENDED, USE X1 OR X2
    #     AS THE NEW X3.
    #-----------------------------------------------------------------------
    df.PX1[i] = (0.897 * X1) + (df.Z[i] / 3.0)
    df.PX1[i] = max(df.PX1[i], 0.0)   
    if df.PX1[i] >= 1.0:   
        
        if df.PX3[i] == 0.0:   
            #         ------------------- IF NO EXISTING WET SPELL OR DROUGHT   
            #                             X1 BECOMES THE NEW X3 
            df.X[i] = df.PX1[i] 
            df.PX3[i] = df.PX1[i] 
            df.PX1[i] = 0.0
            iass = 1
            df = _assign(df, iass, K8, j, m, nendyr, nbegyr)
            K8 = 0
                
            #GOTO 220 in pdinew.f
            V = PV 
            prob_ended = df.PPR[i] 
            X1  = df.PX1[i] 
            X2  = df.PX2[i] 
            X3  = df.PX3[i] 

            return df, X1, X2, X3, V, prob_ended, K8
            
    df.PX2[i] = (0.897 * X2) + (df.Z[i] / 3.0)
    df.PX2[i] = min(df.PX2[i], 0.0)   
    if df.PX2[i] <= -1.0:  
        
        if df.PX3[i] == 0.0:   
            #         ------------------- IF NO EXISTING WET SPELL OR DROUGHT   
            #                             X2 BECOMES THE NEW X3 
            df.X[i]   = df.PX2[i] 
            df.PX3[i] = df.PX2[i] 
            df.PX2[i] = 0.0  
            iass = 2            
            df = _assign(df, iass, K8, j, m, nendyr, nbegyr)
            K8 = 0

            #GOTO 220 in pdinew.f
            V = PV 
            prob_ended = df.PPR[i] 
            X1  = df.PX1[i] 
            X2  = df.PX2[i] 
            X3  = df.PX3[i] 

            return df, X1, X2, X3, V, prob_ended, K8
            
    if df.PX3[i] == 0.0:   
        #    -------------------- NO ESTABLISHED DROUGHT (WET SPELL), BUT X3=0  
        #                         SO EITHER (NONZERO) X1 OR X2 MUST BE USED AS X3   
        if df.PX1[i] == 0.0:   
        
            df.X[i] = df.PX2[i]   
            iass = 2            
            df = _assign(df, iass, K8, j, m, nendyr, nbegyr)
            K8 = 0

            #GOTO 220 in pdinew.f
            V = PV 
            prob_ended = df.PPR[i] 
            X1  = df.PX1[i] 
            X2  = df.PX2[i] 
            X3  = df.PX3[i] 

            return df, X1, X2, X3, V, prob_ended, K8

        elif df.PX2[i] == 0:
            
            df.X[i] = df.PX1[i]   
            iass = 1   
            df = _assign(df, iass, K8, j, m, nendyr, nbegyr)
            K8 = 0

            #GOTO 220 in pdinew.f
            V = PV 
            prob_ended = df.PPR[i] 
            X1  = df.PX1[i] 
            X2  = df.PX2[i] 
            X3  = df.PX3[i] 

            return df, X1, X2, X3, V, prob_ended, K8

    #-----------------------------------------------------------------------
    #     AT THIS POINT THERE IS NO DETERMINED VALUE TO ASSIGN TO X,
    #     ALL VALUES OF X1, X2, AND X3 ARE SAVED IN SX* arrays. AT A LATER  
    #     TIME X3 WILL REACH A VALUE WHERE IT IS THE VALUE OF X (PDSI). 
    #     AT THAT TIME, THE ASSIGN SUBROUTINE BACKTRACKS THROUGH SX* arrays   
    #     CHOOSING THE APPROPRIATE X1 OR X2 TO BE THAT MONTH'S X. 
    #-----------------------------------------------------------------------
    
    #!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #DEVELOPMENT/DEBUG -- REMOVE? this appears to fix some issues with backtracking
    #!!!---- NOTE ---- nothing equivalent is in the original pdinew.f code
    #TODO determine why this kludge is required, it probably will explain other outstanding issues
    # reset the year and month index arrays so that the current j/m month is used as the first backtracking month
    if K8 == 0:
        df.index_j[K8] = j
        df.index_m[K8] = m
    #!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

    df.SX1[K8] = df.PX1[i] 
    df.SX2[K8] = df.PX2[i] 
    df.SX3[K8] = df.PX3[i] 
    df.X[i]    = df.PX3[i] 
    K8 = K8 + 1
    
    #-----------------------------------------------------------------------
    #     SAVE THIS MONTHS CALCULATED VARIABLES (V,PRO,X1,X2,X3) FOR   
    #     USE WITH NEXT MONTHS DATA 
    #-----------------------------------------------------------------------
    V = PV 
    prob_ended = df.PPR[i] 
    X1 = df.PX1[i] 
    X2 = df.PX2[i] 
    X3 = df.PX3[i] 

    return df, X1, X2, X3, V, prob_ended, K8
 
#-----------------------------------------------------------------------------------------------------------------------
# from 210 in pdinew.f
def _between_0s(df,
                K8,
                X3, 
                j, 
                m, 
                nendyr, 
                nbegyr):
    '''
    Compute index values when a weather spell has been established and no abatement is underway. In this case we 
    calculate the X3 for use as the current month's severity index, if no backtracking is called for, and if 
    backtracking is called for then for all the backtracked months we'll use the X3 values computed for those months.
    
    :param df: a pandas DataFrame containing the arrays of various values used within
    :param K8: the number of backtracking steps currently called for, i.e. the number of previous months for which 
               a conclusive severity index has not yet been determined and which require back fill
    :param X3: the previous month's severity index for any weather spell that has become established
    :param j: the year index, assuming that the original climatology datasets are in a 2-D shape (years, months)  
    :param m: the calendar month index, valid range [0..11], assuming that the original climatology datasets
              are in a 2-D shape (years, months)
    :param nendyr: final year of the time series
    :param nbegyr: beginning year of the time series
    :return: seven values: 1) the same pandas DataFrame used as the first argument, now with an updated state 
                           2) the X1 for this month (always 0.0), to be used within the next month's index calculations  
                           3) the X2 for this month (always 0.0), to be used within the next month's index calculations  
                           4) the X3 for this month, to be used within the next month's index calculations
                           5) the accumulated "effective wetness" after this month's accounting (always 0.0)
                           6) the percentage probability that an established weather spell has ended (always 0.0) 
                           7) the number of months requiring back fill via the backtracking process (always 0)
    '''
    # convert year/month (j/m) indices to a series index (2-D -> 1-D)
    i = (j * 12) + m
    
    #-----------------------------------------------------------------------
    #     PROB(END) RETURNS TO 0.  A POSSIBLE ABATEMENT HAS FIZZLED OUT,
    #     SO WE ACCEPT ALL STORED VALUES OF X3  
    #-----------------------------------------------------------------------
    
    # the probability that we've reached the end of a weather spell (PPR, or Pe in Palmer 1965) is zero 
    df.PPR[i] = 0.0
    
    # X3 is the index value in play here since our Pe is zero, so only calculate this month's X3 and use this as the X
    df.PX1[i] = 0.0 
    df.PX2[i] = 0.0 
    df.PX3[i] = (0.897 * X3) + (df.Z[i] / 3.0)
    df.X[i] = df.PX3[i] 
    
    if K8 == 0:  # no backtracking required
        
        df.PDSI[i] = df.X[i]  
        df.PHDI[i] = df.PX3[i] 
        
        if df.PX3[i] == 0.0:
            
            df.PHDI[i] = df.X[i]
        
        df.WPLM[i] = _case(df.PPR[i], df.PX1[i], df.PX2[i], df.PX3[i]) 
        
    else:  # perform backtracking, assigning the stored X3 values as our new backtrack array values

        iass = 3   
        df = _assign(df, iass, K8, j, m, nendyr, nbegyr)
        K8 = 0

    #-----------------------------------------------------------------------------------------------
    #     SAVE THIS MONTHS CALCULATED VARIABLES (V, PRO, X1, X2, X3) FOR USE WITH NEXT MONTHS DATA 
    #-----------------------------------------------------------------------------------------------
    
    # accumulated effective wetness reset to zero
    V = 0.0
    X1 = df.PX1[i] 
    X2 = df.PX2[i] 
    X3 = df.PX3[i] 
    prob_ended = df.PPR[i] 

    return df, X1, X2, X3, V, prob_ended, K8

#-----------------------------------------------------------------------------------------------------------------------
def _assign(df,
            iass,
            K8,
            j,
            m,
            nendyr,
            nbegyr):

    '''
    :df pandas DataFrame
    :param iass:
    :param K8:  
    :param j:
    :param m:
    :param nendyr:
    :param nbegyr:    
     '''

    # convert the 2-D time step indices (j, m) to a series index i
    i = (j * 12) + m
    
    # flag to determine which of the SX* values to save into the main backtracking array, df.SX
    ISAVE = iass

    #   
    #-----------------------------------------------------------------------
    #     FIRST FINISH OFF FILE 8 WITH LATEST VALUES OF PX3, Z,X
    #     X=PX1 FOR I=1, PX2 FOR I=2, PX3,  FOR I=3 
    #-----------------------------------------------------------------------
    df.SX[K8] = df.X[i]
     
    if K8 == 0:  # no backtracking required

        # set the PDSI to X
        df.PDSI[i] = df.X[i]  
    
        # the PHDI is X3 if not zero, otherwise use X
        df.PHDI[i] = df.PX3[i]         
        if df.PX3[i] == 0.0:
            df.PHDI[i] = df.X[i]
        
        # select the best fit for WPLM
        df.WPLM[i] = _case(df.PPR[i], df.PX1[i], df.PX2[i], df.PX3[i]) 

    else:  # perform backtracking
        
        if iass == 3:  
            #     ---------------- USE ALL X3 VALUES

            for Mm in range(K8):
        
                df.SX[Mm] = df.SX3[Mm]
    
        else: 
            #     -------------- BACKTRACK THRU ARRAYS, STORING ASSIGNED X1 (OR X2) 
            #                    IN SX UNTIL IT IS ZERO, THEN SWITCHING TO THE OTHER
            #                    UNTIL IT IS ZERO, ETC.
            for Mm in range(K8 - 1, -1, -1):

                if ISAVE == 1: # then GO TO 20 in pdinew.f
                    
                    # we should assign the stored X1 values into the main backtracking array until 
                    # we find a stored X1 that is zero, which indicates a state change (wet to dry)
                    
                    if df.SX1[Mm] == 0:  # then GO TO 50 in pdinew.f
                        
                        # the X1, or severity index for a wet spell that's being established, is zero 
                        # for this backtrack month, indicating that we need to switch to using the X2 values
                        ISAVE = 2
                        df.SX[Mm] = df.SX2[Mm]
                    
                    else:
                        # assign the stored X1 into this backtrack month's position within the main backtracking array
                        df.SX[Mm] = df.SX1[Mm]
                
                elif ISAVE == 2: # then GO TO 40 in pdinew.f
                    
                    # we should assign the stored X2 values into the main backtracking array until 
                    # we find a stored X2 that is zero, which indicates a state change (dry to wet)
                    
                    if df.SX2[Mm] == 0: # then GO TO 30 in pdinew.f
                        
                        # the X2, or severity index for a dry spell that's being established, is zero 
                        # for this backtrack month, indicating that we need to switch to using the X1 values
                        ISAVE = 1
                        df.SX[Mm] = df.SX1[Mm]
                        
                    else:
                        
                        # assign the stored X2 into this backtrack month's position within the main backtracking array
                        df.SX[Mm] = df.SX2[Mm]
    
        # label 70 from pdinew.f
        #-----------------------------------------------------------------------
        #     PROPER ASSIGNMENTS TO ARRAY SX HAVE BEEN MADE,
        #     OUTPUT THE MESS   
        #-----------------------------------------------------------------------
        
        for n in range(K8 + 1):   # backtracking assignment of X
                    
            # get the 1-D index equivalent to the j/m index for the final arrays 
            # (PDSI, PHDI, etc.) that we'll assign to in the backtracking process
            ix = (df.index_j[n] * 12) + df.index_m[n]
            
            # assign the backtracking array's value for the current backtrack month as that month's final PDSI value
            df.PDSI[ix] = df.SX[n] 

            #!!!!!!!!!!!!!!!!!!!!!!!!     Debugging section below -- remove before deployment
            #
            # show backtracking array contents and describe differences
            tolerance = 0.01            
            if math.isnan(df.PDSI[ix]) or (abs(df.expected_pdsi[ix] - df.PDSI[ix]) > tolerance):
                print('~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~')
                print('\nBACKTRACKING  actual time step:  {0}\tBacktracking index: {1}'.format(i, ix))
                print('\tNumber of backtracking steps (K8):  {0}'.format(K8))
                print('\tPDSI:  Expected {0:.2f}\n\t       Actual: {1:.2f}'.format(df.expected_pdsi[ix], 
                                                                                   df.PDSI[ix]))
                print('\nSX: {0}'.format(df.SX._values[0:K8]))
                print('SX1: {0}'.format(df.SX1._values[0:K8]))
                print('SX2: {0}'.format(df.SX2._values[0:K8]))
                print('SX3: {0}'.format(df.SX3._values[0:K8]))
                print('~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~')
            #!!!!!!!!!----------- cut here -------------------------------------------------------
                    
            # the PHDI is X3 if not zero, otherwise use X
            #TODO literature reference for this?
            df.PHDI[ix] = df.PX3[ix]
            if df.PX3[ix] == 0.0:
                df.PHDI[ix] = df.SX[n]
                
            # select the best fit for WPLM
            df.WPLM[ix] = _case(df.PPR[ix],
                                df.PX1[ix], 
                                df.PX2[ix],
                                df.PX3[ix])

    return df

#-----------------------------------------------------------------------------------------------------------------------
def _case(PROB,
          X1,
          X2,
          X3):
    #   
    #     THIS SUBROUTINE SELECTS THE PRELIMINARY (OR NEAR-REAL TIME)   
    #     PALMER DROUGHT SEVERITY INDEX (PDSI) FROM THE GIVEN X VALUES  
    #     DEFINED BELOW AND THE PROBABILITY (PROB) OF ENDING EITHER A   
    #     DROUGHT OR WET SPELL. 
    #   
    #     X1   - INDEX FOR INCIPIENT WET SPELLS (ALWAYS POSITIVE)   
    #     X2   - INDEX FOR INCIPIENT DRY SPELLS (ALWAYS NEGATIVE)   
    #     X3   - SEVERITY INDEX FOR AN ESTABLISHED WET SPELL (POSITIVE) OR DROUGHT (NEGATIVE)  
    #     PDSI - THE SELECTED PDSI (EITHER PRELIMINARY OR FINAL)
    #   
    #   This subroutine written and provided by CPC (Tom Heddinghaus & Paul Sabol).
    #   

    if X3 == 0.0: #) GO TO 10 in pdinew.f
        #     IF X3=0 THE INDEX IS NEAR NORMAL AND EITHER A DRY OR WET SPELL
        #     EXISTS.  CHOOSE THE LARGEST ABSOLUTE VALUE OF X1 OR X2.  
        PDSI = X1
        if abs(X2) > abs(X1): 
            PDSI = X2

    elif  PROB > 0.0 and PROB < 100.0: # GO TO 20 in pdinew.f

        # put the probability value into 0..1 range
        probability = PROB / 100.0
        if X3 > 0.0: #) GO TO 30
            #     TAKE THE WEIGHTED SUM OF X3 AND X2
            PDSI = (1.0 - probability) * X3 + probability * X2   
        else:  
            #     TAKE THE WEIGHTED SUM OF X3 AND X1
            PDSI = (1.0 - probability) * X3 + probability * X1   

    else:
        #     A WEATHER SPELL IS ESTABLISHED AND PDSI=X3 AND IS FINAL
        PDSI = X3
 
    return PDSI

#-----------------------------------------------------------------------------------------------------------------------
def display_debug_info(df,
                       i,
                       j,
                       m,
                       K8):
    pass
#     if i >= 2:
#         irange = 2
#     elif i == 1:
#         irange = 1
#     else:
#         irange = 0
#          
#     print('Index: {0}'.format(i))
#     print('J: {0}'.format(j))
#     print('M: {0}'.format(m))
#     print('\nPDSI:\n{0}'.format(df.PDSI.values[i-irange:i+irange]))
#     print('\nExpected:\n{0}'.format(df.expected_pdsi.values[i-irange:i+irange]))    
#     print('\nK8: {0}'.format(K8))
#     print('SX:\n{0}'.format(df.SX.values[0:K8]))
#     print('SX1:\n{0}'.format(df.SX1.values[0:K8]))
#     print('SX2:\n{0}'.format(df.SX2.values[0:K8]))
#     print('SX3:\n{0}'.format(df.SX3.values[0:K8]))
#     print('\nIndexJ:\t{0}'.format(df.index_j.values[0:K8]))
#     print('IndexM:\t{0}'.format(df.index_m.values[0:K8]))
    