import json
import os
import uuid
from collections import namedtuple, OrderedDict
import tornado.escape
import tornado.httpserver
import tornado.ioloop
import tornado.options
import tornado.web
from tornado.options import define, options

import traceback
from sqlalchemy import *
from concurrent.futures import ProcessPoolExecutor
import pickle
import pandas as pd 
import numpy as np
import requests
from astroquery.simbad import Simbad
import astropy.units as u

from .pandexo import wrapper
from .utils.plotters import create_component_jwst, create_component_hst
from .logs import jwst_log, hst_log
from .exomast import get_target_data


# define location of temp files
__TEMP__ = os.environ.get("PANDEXO_TEMP", os.path.join(os.path.dirname(__file__), "temp"))

#define location of fort grids
try:
    __FORT__ = os.environ.get('FORTGRID_DIR')
    db_fort = create_engine('sqlite:///'+__FORT__)
except: 
    print('FORTNEY DATABASE NOT INSTALLED')

#add Simbad query info 
Simbad.add_votable_fields('flux(H)')
Simbad.add_votable_fields('flux(J)')

define("port", default=1111, help="run on the given port", type=int)
define("debug", default=False, help="automatically detect code changes in development")
define("workers", default=4, help="maximum number of simultaneous async tasks")

# Define a simple named tuple to keep track for submitted calculations
CalculationTask = namedtuple('CalculationTask', ['id', 'name', 'task',
                                                 'cookie', 'count'])


class Application(tornado.web.Application):
    """Gobal settings of the server
    This defines the global settings of the server. This parses out the
    handlers, and includes settings for if ever we want to tie this to a
    database.
    """
    def __init__(self):
        handlers = [
            (r"/", HomeHandler),
            (r"/about", AboutHandler),
            (r"/dashboard", DashboardHandler),
            (r"/dashboardhst", DashboardHSTHandler),
            (r"/tables", TablesHandler),
            (r"/helpfulplots", HelpfulPlotsHandler),
            (r"/calculation/new", CalculationNewHandler),
            (r"/calculation/newHST", CalculationNewHSTHandler),
            (r"/calculation/status/([^/]+)", CalculationStatusHandler),
            (r"/calculation/statushst/([^/]+)", CalculationStatusHSTHandler),
            (r"/calculation/view/([^/]+)", CalculationViewHandler),
            (r"/calculation/viewhst/([^/]+)", CalculationViewHSTHandler),
            (r"/calculation/download/([^/]+)", CalculationDownloadHandler),
            (r"/calculation/downloadpandin/([^/]+)", CalculationDownloadPandInHandler)
        ]
        settings = dict(
            blog_title="Pandexo",
            template_path=os.path.join(os.path.dirname(__file__), "templates"),
            static_path=os.path.join(os.path.dirname(__file__), "static"),
            xsrf_cookies=True,
            cookie_secret="__TODO:_GENERATE_YOUR_OWN_RANDOM_VALUE_HERE__",
            debug=True,
        )
        super(Application, self).__init__(handlers, **settings)


class BaseHandler(tornado.web.RequestHandler):
    """
    Logic to handle user information and database access might go here.
    """
    executor = ProcessPoolExecutor(max_workers=16)
    buffer = OrderedDict()

    def _get_task_response(self, id):
        """
        Simple function to grab a calculation that's stored in the buffer,
        and return a dictionary/json-like response to the front-end.
        """
        calc_task = self.buffer.get(id)
        task = calc_task.task

        response = {'id': id,
                    'name': calc_task.name,
                    'count': calc_task.count}

        if task.running():
            response['state'] = 'running'
            response['code'] = 202
        elif task.done():
            response['state'] = 'finished'
        elif task.cancelled():
            response['state'] = 'cancelled'
        else:
            response['state'] = 'pending'

        response['html'] = tornado.escape.to_basestring(
            self.render_string("calc_row.html", response=response))
        return response

 
    def _get_task_response_hst(self, id):
        """
        Simple function to grab a calculation that's stored in the buffer,
        and return a dictionary/json-like response to the front-end.
        """
        calc_task = self.buffer.get(id)
        task = calc_task.task

        response = {'id': id,
                    'name': calc_task.name,
                    'count': calc_task.count}

        if task.running():
            response['state'] = 'running'
            response['code'] = 202
        elif task.done():
            response['state'] = 'finished'
        elif task.cancelled():
            response['state'] = 'cancelled'
        else:
            response['state'] = 'pending'

        response['html'] = tornado.escape.to_basestring(
            self.render_string("calc_rowhst.html", response=response))

        return response
        
    def write_error(self, status_code, **kwargs):
        """
        This renders a customized error page
        """
        reason = self._reason
        error_info = ''
        trace_print = ''
        if 'exc_info' in kwargs:
            error_info = kwargs['exc_info']
            try:
                trace_print = traceback.format_exception(*error_info)
                trace_print = "\n".join(map(str,trace_print))
            except:
                pass
        self.render('errors.html',page=None, status_code=status_code, reason=reason, error_log=trace_print)


    def _get_task_result(self, id):
        """
        This method grabs only the result returned from the python `Future`
        object. This contains the stuff that Pandeia returns.
        """
        calc_task = self.buffer.get(id)
        task = calc_task.task

        return task.result()

    def _add_task(self, id, name, task):
        """
        This creates the task and adds it to the buffer.
        """
        self.buffer[id] = CalculationTask(id=id, name=name, task=task,
                                          count=len(self.buffer)+1,
                                          cookie=self.get_cookie("pandexo_user"))

        # Only allow 100 tasks **globally**. This will delete old tasks first.
        if len(self.buffer) > 100:
            self.buffer.popitem(last=False)


class HomeHandler(BaseHandler):
    def get(self):
        """
        This sets an **unsecured** cookie. If user accounts gets
        implemented, this must be changed to a secure cookie.
        """
        if not self.get_cookie("pandexo_user"):
            self.set_cookie("pandexo_user", str(uuid.uuid4()))

        self.render("home.html")
        
class AboutHandler(BaseHandler):
    def get(self):
        """
        Render about PandExo Page
        """
        self.render("about.html")

class TablesHandler(BaseHandler):
    def get(self):
        """
        Render tables with confirmed candidates
        """
        self.render("tables.html")

class HelpfulPlotsHandler(BaseHandler):
    def get(self):
        """
        Renders helpful bokeh plots
        """
        self.render("helpfulplots.html")


class DashboardHandler(BaseHandler):
    """
    Request handler for the dashboard page. This will retrieve and render
    the html template, along with the list of current task objects.
    """
    def get(self):
        task_responses = [self._get_task_response(id) for id, nt in
                          list(self.buffer.items())
                          if ((nt.cookie == self.get_cookie("pandexo_user"))
                          & (id[len(id)-1]=='e'))]
        
        self.render("dashboard.html", calculations=task_responses[::-1])


class DashboardHSTHandler(BaseHandler):
    """
    Request handler for the dashboard page. This will retrieve and render
    the html template, along with the list of current task objects.
    """
    def get(self):
        task_responses = [self._get_task_response_hst(id) for id, nt in
                          list(self.buffer.items())
                          if ((nt.cookie == self.get_cookie("pandexo_user"))
                          & (id[len(id)-1]=='h'))]
        
        self.render("dashboardhst.html", calculations=task_responses[::-1])


class CalculationNewHandler(BaseHandler):
    """
    This request handler deals with processing the form data and submitting
    a new calculation task to the parallelized workers.
    """
    def get(self):
        try: 
            header= pd.read_sql_table('header',db_fort)
        except:
            header = pd.DataFrame({
            'temp': ['NO GRID DB FOUND'],
            'ray' : ['NO GRID DB FOUND'],
            'flat':['NO GRID DB FOUND']})

        with open(os.path.join(os.path.dirname(__file__), "reference",
                               "exo_input.json")) as data_file:
            exodata = json.load(data_file)

        all_planets =  requests.get("https://exoplanetarchive.ipac.caltech.edu/cgi-bin/nstedAPI/nph-nstedAPI?table=exoplanets&select=pl_name&format=csv")  
        all_planets = all_planets.text.replace(' ','').split('\n')[1:]

        self.render("new.html", id=id,
                                 temp=list(map(str, header.temp.unique())), 
                                 planets=all_planets,
                                 data=exodata)

    def post(self):
        """
        The post method contains the returned data from the form data (
        accessed by using `self.get_argument(...)` for specific arguments,
        or `self.request.body` to grab the entire returned object.
        """
        
        #print(self.request.body)
        
        id = str(uuid.uuid4())+'e'
        
        exomast_response = self.get_argument("resolve_target", None)
        submit_form = self.get_argument("submit_form", None)

        if exomast_response=="exomast":
            with open(os.path.join(os.path.dirname(__file__), "reference",
                            "exo_input.json")) as data_file:
                exodata = json.load(data_file)
                exodata["telescope"] = 'jwst'
                exodata["calculation"] = 'fml'  # always for online form

            try:
                planet_name = self.get_argument("planetname")
                planet_data = get_target_data(planet_name)[0]
                exodata['planet']['planetname'] = planet_data['canonical_name']
                # for item in planet_data:
                #     print("{}: {}".format(item, planet_data[item]))
                # star
                exodata["star"]["temp"] = planet_data['Teff']
                exodata["star"]["logg"] = planet_data['stellar_gravity']
                exodata["star"]["metal"] = planet_data['Fe/H'] 
                # Keep Simbad query, as exoMAST typically does not have Jmag:
                exodata["star"]["jmag"] = Simbad.query_object(planet_name[:-1])['FLUX_J'][0] #planet_data['Jmag']
                exodata["star"]["ref_wave"] = 1.25

                # optional star radius
                exodata["star"]["radius"] = planet_data['Rs']  
                exodata["star"]["r_unit"] = planet_data['Rs_unit'][0]+ planet_data['Rs_unit'][1:].lower()    
 
                # optional planet radius/mass
                exodata["planet"]["radius"] = planet_data['Rp']  
                exodata["planet"]["r_unit"] = planet_data['Rp_unit'][0]+ planet_data['Rp_unit'][1:].lower()  
                exodata["planet"]["mass"] = planet_data['Mp'] 
                exodata["planet"]["m_unit"] = planet_data['Mp_unit'][0]+ planet_data['Mp_unit'][1:].lower() 
                exodata["planet"]["transit_duration"] = planet_data['transit_duration']
                exodata["planet"]["td_unit"] = planet_data['transit_duration_unit']

                if planet_data['inclination'] == None:   
                    inc = 90
                else: 
                    inc = planet_data['inclination']

                exodata["planet"]["i"]          = inc
                exodata["planet"]["ars"]        = planet_data['a/Rs'] 
                period = planet_data['orbital_period'] 
                period_unit = planet_data['orbital_period_unit'] 
                exodata["planet"]["period"]     = (period*u.Unit(period_unit)).to(u.Unit('day')).value
                exodata["planet"]["ecc"]        = planet_data['eccentricity'] 
                try:
                    exodata["planet"]["w"]      = float(planet_data['omega'] )
                except: 
                    exodata["planet"]["w"]      = 90.

                self.header = pd.DataFrame({'temp': ['NO GRID DB FOUND'],
                                    'ray' : ['NO GRID DB FOUND'],
                                    'flat':['NO GRID DB FOUND']})
            except:
                exodata['url_err'] = 'Sorry, cant resolve target {}'.format(planet_name)

            all_planets =  requests.get("https://exoplanetarchive.ipac.caltech.edu/cgi-bin/nstedAPI/nph-nstedAPI?table=exoplanets&select=pl_name&format=csv")  
            all_planets = all_planets.text.replace(' ','').split('\n')[1:]

            self.header = pd.DataFrame({'temp': ['NO GRID DB FOUND'],
                                    'ray' : ['NO GRID DB FOUND'],
                                    'flat':['NO GRID DB FOUND']})

            return self.render("new.html", id=id,
                                temp=list(map(str, self.header.temp.unique())),
                                data=exodata,
                                planets=all_planets)


        if submit_form == 'submit':
            with open(os.path.join(os.path.dirname(__file__), "reference",
                            "exo_input.json")) as data_file:
                exodata = json.load(data_file)

                exodata["telescope"] = 'jwst'
                exodata["calculation"] = 'fml'

                # Planet Data
                exodata['planet']['planetname'] = self.get_argument("planetname", None)
                exodata['planet']['transit_duration'] = float(self.get_argument("transit_duration", None))
                exodata['planet']['radius'] = float(self.get_argument("refradc", None))
                exodata['planet']['r_unit'] = str(self.get_argument("r_unitc")) + 'iter'

                # Star Data
                exodata['star']['jmag'] = float(self.get_argument("Jmag", None))
                exodata['star']['temp'] = float(self.get_argument("temp", None))
                exodata['star']['radius'] = float(self.get_argument("rstarc", None))
                exodata['star']['r_unit'] = str(self.get_argument("rstar_unitc"))

            # planet model
            # exodata["planet"]["type"] = self.get_argument("planetModel")
            if exodata["planet"]["type"] == "user":
                # process planet file
                fileinfo_plan = self.request.files['planFile'][0]
                fname_plan = fileinfo_plan['filename']
                extn_plan = os.path.splitext(fname_plan)[1]
                cname_plan = id+'planet' + extn_plan
                fh_plan = open(os.path.join(__TEMP__, cname_plan), 'wb')
                fh_plan.write(fileinfo_plan['body'])

                exodata["planet"]["exopath"] = os.path.join(__TEMP__, cname_plan)
                exodata["planet"]["w_unit"] = self.get_argument("planwunits")
                exodata["planet"]["f_unit"] = self.get_argument("planfunits")
            elif exodata["planet"]["type"] == "constant":                               
                if self.get_argument("constant_unit") == 'fp/f*':
                    exodata["planet"]["temp"] = float(self.get_argument("ptempc"))
                    exodata["planet"]["f_unit"] = 'fp/f*'
                elif self.get_argument("constant_unit") == 'rp^2/r*^2':
                    exodata["planet"]["f_unit"] = 'rp^2/r*^2'
            elif exodata["planet"]["type"] == "grid":
                exodata["planet"]["temp"] = float(self.get_argument("ptempg"))
                exodata["planet"]["chem"] = str(self.get_argument("pchem"))
                exodata["planet"]["cloud"] = self.get_argument("cloud") 
                exodata["planet"]["mass"] = float(self.get_argument("pmass"))
                exodata["planet"]["m_unit"] = str(self.get_argument("m_unit"))
            #baseline 
            exodata["observation"]["baseline"] = float(self.get_argument("baseline"))
            exodata["observation"]["baseline_unit"] = self.get_argument("baseline_unit")
            try:
                exodata["observation"]["target_acq"] = self.get_argument("TA") == 'on'
            except:
                exodata["observation"]["target_acq"] = False
                
            exodata["observation"]["noccultations"] = float(self.get_argument("numtrans"))
            exodata["observation"]["sat_level"] = float(self.get_argument("satlevel"))
            exodata["observation"]["sat_unit"] = self.get_argument("sat_unit")

                
            # noise floor, set to 0.0 of no values are input
            try:
                observation_type = self.get_argument("noiseModel")
                if observation_type == "user":
                    # process noise file
                    fileinfo_noise = self.request.files['noiseFile'][0]
                    fname_noise = fileinfo_noise['filename']
                    extn_noise = os.path.splitext(fname_noise)[1]
                    cname_noise = id + 'noise' + extn_noise
                    fh_noise = open(os.path.join(__TEMP__, cname_noise), 'wb')
                    fh_noise.write(fileinfo_star['body'])
                    exodata["observation"]["noise_floor"] = os.path.join(__TEMP__, cname_noise)
                else:
                    exodata["observation"]["noise_floor"] = float(self.get_argument("noisefloor"))
            except:
                exodata["observation"]["noise_floor"] = 0.0

        instrument = self.get_argument("instrument").lower()
        if instrument == "miri":
            with open(os.path.join(os.path.dirname(__file__), "reference", "miri_input.json")) as data_file:
                pandata = json.load(data_file)       
                mirimode = self.get_argument("mirimode")
                if (mirimode == "lrsslit"):
                    pandata["configuration"]["instrument"]["mode"] = mirimode
                    pandata["configuration"]["instrument"]["aperture"] = "lrsslit"
                    pandata["configuration"]["detector"]["subarray"] = "full"

        if instrument == "nirspec":
            with open(os.path.join(os.path.dirname(__file__), "reference", "nirspec_input.json")) as data_file:
                pandata = json.load(data_file)  
                nirspecmode = self.get_argument("nirspecmode")
                pandata["configuration"]["instrument"]["disperser"] = nirspecmode[0:5]
                pandata["configuration"]["instrument"]["filter"] = nirspecmode[5:11]
                pandata["configuration"]["detector"]["subarray"] = self.get_argument("nirspecsubarray")

        if instrument == "nircam":
            with open(os.path.join(os.path.dirname(__file__), "reference", "nircam_input.json")) as data_file:
                pandata = json.load(data_file) 
                pandata["configuration"]["instrument"]["filter"] = self.get_argument("nircammode")
                pandata["configuration"]["detector"]["subarray"] = self.get_argument("nircamsubarray")

        if instrument == "niriss":
            with open(os.path.join(os.path.dirname(__file__), "reference", "niriss_input.json")) as data_file:
                pandata = json.load(data_file)
                nirissmode = self.get_argument("nirissmode")
                pandata["configuration"]["detector"]["subarray"] = nirissmode

        pandata['configuration']['instrument']['instrument'] = instrument
        
        # write in optimal groups or set a number
        try:
            pandata["configuration"]["detector"]["ngroup"] = int(self.get_argument("optimize"))
        except: 
            pandata["configuration"]["detector"]["ngroup"] = self.get_argument("optimize")
        
        finaldata = {"pandeia_input": pandata, "pandexo_input": exodata}

        #PandExo stats
        try: 
            jwst_log(finaldata)
        except: 
            pass

        task = self.executor.submit(wrapper, finaldata)


        self._add_task(id, self.get_argument("calcName"), task)

        response = self._get_task_response(id)
        response['info'] = {}
        response['location'] = '/calculation/status/{}'.format(id)
        
        
        self.write(dict(response))
        self.redirect("../dashboard")
        
    
class CalculationNewHSTHandler(BaseHandler):
    """
    This request handler deals with processing the form data and submitting
    a new HST calculation task to the parallelized workers.
    """


    def get(self):
        try: 
            self.header= pd.read_sql_table('header',db_fort)
        except:
            self.header = pd.DataFrame({
            'temp': ['NO GRID DB FOUND'],
            'ray' : ['NO GRID DB FOUND'],
            'flat':['NO GRID DB FOUND']})
        with open(os.path.join(os.path.dirname(__file__), "reference",
                               "exo_input.json")) as data_file:
            exodata = json.load(data_file)
        all_planets =  requests.get("https://exoplanetarchive.ipac.caltech.edu/cgi-bin/nstedAPI/nph-nstedAPI?table=exoplanets&select=pl_name&format=csv")  
        all_planets = all_planets.text.replace(' ','').split('\n')[1:]
        self.render("newHST.html", id=id,
                    temp=list(map(str, self.header.temp.unique())),
                    data=exodata,
                    planets=all_planets)

    def post(self):
        """
        The post method contains the retured data from the form data (
        accessed by using `self.get_argument(...)` for specific arguments,
        or `self.request.body` to grab the entire returned object.
        """
        
        # print(self.request.body)

        id = str(uuid.uuid4())+'h'

        exomast_response = self.get_argument("resolve_target", None)
        
        submit_form = self.get_argument("submit_form", None)

        if exomast_response=="exomast":
            with open(os.path.join(os.path.dirname(__file__), "reference",
                            "exo_input.json")) as data_file:
                exodata = json.load(data_file)
                exodata["telescope"] = 'hst'

            try:
                planet_name = self.get_argument("planetname")
                planet_data, url = get_target_data(planet_name)

                exodata['planet']['planetname'] = planet_data['canonical_name']
                exodata['url'] = url
                
                #star
                exodata["star"]["temp"] = planet_data['Teff']

                jmag = Simbad.query_object(planet_name[:-1])['FLUX_J'][0]
                hmag = Simbad.query_object(planet_name[:-1])['FLUX_H'][0]

                exodata["star"]["jmag"] = jmag
                exodata["star"]["hmag"] = hmag

                if exodata["telescope"] == 'hst':
                    exodata["star"]["mag"] = hmag

                #optinoal star radius
                exodata["star"]["radius"] = planet_data['Rs']  
                exodata["star"]["r_unit"] = planet_data['Rs_unit'][0]+ planet_data['Rs_unit'][1:].lower()    

                #optional planet radius/mass
                exodata["planet"]["radius"] = planet_data['Rp']
                exodata["planet"]["r_unit"] = planet_data['Rp_unit'][0]+ planet_data['Rp_unit'][1:].lower()  
                exodata["planet"]["mass"] = planet_data['Mp'] 
                exodata["planet"]["m_unit"] = planet_data['Mp_unit'][0]+ planet_data['Mp_unit'][1:].lower()  

                transit_duration = planet_data['transit_duration'] 
                td_unit = planet_data['transit_duration_unit'] 
                transit_duration  = (transit_duration*u.Unit(td_unit)).to(u.Unit('day')).value
                exodata["planet"]["transit_duration"] = transit_duration
                
                if planet_data['inclination'] == None:   
                    inc = 90
                else: 
                    inc = planet_data['inclination']

                exodata["planet"]["i"]          = inc
                exodata["planet"]["ars"]        = planet_data['a/Rs'] 
                period = planet_data['orbital_period'] 
                period_unit = planet_data['orbital_period_unit'] 
                exodata["planet"]["period"]     = (period*u.Unit(period_unit)).to(u.Unit('day')).value
                exodata["planet"]["ecc"]        = planet_data['eccentricity'] 
                try:
                    exodata["planet"]["w"]      = float(planet_data['omega'] )
                except: 
                    exodata["planet"]["w"]      = 90.

                self.header = pd.DataFrame({'temp': ['NO GRID DB FOUND'],
                                    'ray' : ['NO GRID DB FOUND'],
                                    'flat':['NO GRID DB FOUND']})
            except:
                exodata['url_err'] = 'Sorry, cant resolve target {}'.format(planet_name)

            all_planets =  requests.get("https://exoplanetarchive.ipac.caltech.edu/cgi-bin/nstedAPI/nph-nstedAPI?table=exoplanets&select=pl_name&format=csv")  
            all_planets = all_planets.text.replace(' ','').split('\n')[1:]

            return self.render("newHST.html", id=id,
                                temp=list(map(str, self.header.temp.unique())),
                                data=exodata,
                                planets=all_planets)

        if submit_form == 'submit':
            with open(os.path.join(os.path.dirname(__file__), "reference",
                            "exo_input.json")) as data_file:
                exodata = json.load(data_file)
                exodata["telescope"] = 'hst'

                exodata['planet']['planetname'] = str(self.get_argument("planetname", None))
                exodata['planet']['transit_duration'] = float(self.get_argument("transit_duration", None))
                exodata['planet']['i'] = float(self.get_argument("i", None))
                exodata['planet']['ars'] = float(self.get_argument("ars", None))
                exodata['planet']['period'] = float(self.get_argument("period", None))
                exodata['planet']['ecc'] = float(self.get_argument("ecc", None))
                exodata['planet']['w'] = float(self.get_argument("w", None))
                exodata['planet']['radius'] = float(self.get_argument("refradc", None))
                exodata['planet']['r_unit'] = str(self.get_argument("r_unitc")) + 'iter'

                exodata['star']['jmag'] = float(self.get_argument("Jmag", None))
                exodata['star']['hmag'] = float(self.get_argument("Hmag", None))
                exodata['star']['temp'] = float(self.get_argument("stempc", None))
                exodata['star']['radius'] = float(self.get_argument("rstarc", None))
                exodata['star']['r_unit'] = str(self.get_argument("rstar_unitc"))

                depth = exodata["planet"]["radius"]**2 / ((exodata["star"]["radius"]
                                                            *u.Unit(exodata["star"]["r_unit"]) )
                                                                .to(u.Unit(exodata["planet"]["r_unit"]))).value**2
                exodata["planet"]["depth"] = depth

            # planet model
            # exodata["planet"]["type"] = self.get_argument("planetModel")

            if exodata["planet"]["type"] == "user":
                # process planet file
                fileinfo_plan = self.request.files['planFile'][0]
                fname_plan = fileinfo_plan['filename']
                extn_plan = os.path.splitext(fname_plan)[1]
                cname_plan = id+'planet' + extn_plan
                fh_plan = open(os.path.join(__TEMP__, cname_plan), 'wb')
                fh_plan.write(fileinfo_plan['body'])

                exodata["planet"]["exopath"] = os.path.join(__TEMP__, cname_plan)
                exodata["planet"]["w_unit"] = self.get_argument("planwunits")
                exodata["planet"]["f_unit"] = self.get_argument("planfunits")

            elif exodata["planet"]["type"] == "constant":                               
                if self.get_argument("constant_unit") == 'fp/f*':
                    exodata["planet"]["temp"] = float(self.get_argument("ptempc"))
                    exodata["planet"]["f_unit"] = 'fp/f*'
                elif self.get_argument("constant_unit") == 'rp^2/r*^2':
                    exodata["planet"]["f_unit"] = 'rp^2/r*^2'

            elif exodata["planet"]["type"] == "grid":
                exodata["planet"]["mass"] = float(self.get_argument("pmass"))
                exodata["planet"]["m_unit"] = str(self.get_argument("m_unit"))
                exodata["planet"]["temp"] = float(self.get_argument("ptempg"))
                exodata["planet"]["chem"] = str(self.get_argument("pchem"))
                exodata["planet"]["cloud"] = self.get_argument("cloud") 

            exodata["observation"]["noise_floor"]           = 0.0
            exodata["calculation"]                          = 'scale'
                
        if (self.get_argument("instrument")=="STIS"): 
            with open(os.path.join(os.path.dirname(__file__), "reference",
                               "stis_input.json")) as data_file:   
                pandata = json.load(data_file)       
                stismode = self.get_argument("stismode")
        if (self.get_argument("instrument")=="WFC3"): 
            with open(os.path.join(os.path.dirname(__file__), "reference",
                               "wfc3_input.json")) as data_file:
                pandata = json.load(data_file)  
                pandata["configuration"]['detector']['subarray']    = self.get_argument("subarray")
                pandata["configuration"]['detector']['nsamp']       = int(self.get_argument("nsamp"))
                pandata["configuration"]['detector']['samp_seq']    = self.get_argument("samp_seq")
                pandata["configuration"]['instrument']['disperser'] = self.get_argument("wfc3mode")
            try: 
                pandata["strategy"]["norbits"]           = int(self.get_argument("norbits"))
            except:
                pandata["strategy"]["norbits"]           = None
            exodata["observation"]["noccultations"]         = int(self.get_argument("noccultations"))
            pandata["strategy"]["nchan"]                 = int(self.get_argument("nchan"))
            pandata["strategy"]["scanDirection"]         = self.get_argument("scanDirection")
            pandata["strategy"]["useFirstOrbit"]         = self.get_argument("useFirstOrbit").lower() == 'true'
            try:
                pandata["strategy"]["windowSize"]        = float(self.get_argument("windowSize"))
            except:
                pandata["strategy"]["windowSize"]        = 20.
            pandata["strategy"]["schedulability"]           = self.get_argument("schedulability")
        try:
            calc_ramp = self.get_argument("ramp")

            calc_ramp = True
        except: 
            calc_ramp = False


        pandata['strategy']['calculateRamp'] = calc_ramp
        pandata['strategy']['targetFluence'] = float(self.get_argument("targetFluence"))
		
        #import pickle as pk 
        #a = pk.load(open('/Users/natashabatalha/Desktop/JWST/testing/ui.pk','rb'))
        #pandata = a['pandeia_input']
        #exodata  = a['pandexo_input']

        finaldata = {"pandeia_input": pandata , "pandexo_input":exodata}
        #PandExo stats
        try: 
            hst_log(finaldata)
        except: 
            pass

        task = self.executor.submit(wrapper, finaldata)

        self._add_task(id, self.get_argument("calcName"), task)

        response = self._get_task_response_hst(id)
        response['info'] = {}
        response['location'] = '/calculation/statushst/{}'.format(id)
        
        print('Dashboard')
        print(exodata["planet"])
        self.write(dict(response))
        self.redirect("../dashboardhst")


class CalculationStatusHandler(BaseHandler):
    """
    Handlers returning the status of a particular JWST calculation task.
    """
    def get(self, id):
        response = self._get_task_response(id)

        if self.request.connection.stream.closed():
            return

        self.write(dict(response))

        
class CalculationStatusHSTHandler(BaseHandler):
    """
    Handlers returning the status of a particular HST calculation task.
    """
    def get(self, id):
        response = self._get_task_response_hst(id)

        if self.request.connection.stream.closed():
            return

        self.write(dict(response))                

class CalculationDownloadHandler(BaseHandler):
    """
    Handlers returning the downloaded data of a particular calculation task.
    Handlers returning the status of a particular calculation task.
    """
    def get(self, id):
        result = self._get_task_result(id)
  
        if self.request.connection.stream.closed():
            return
        file_name = "ETC-calculation" +id+".p"
 
        with open(os.path.join(__TEMP__,file_name), "wb") as f:
            pickle.dump(result, f)
 
        buf_size = 4096
        self.set_header('Content-Type', 'application/octet-stream')
        self.set_header('Content-Disposition',
                        'attachment; filename=' + file_name)
 
        with open(os.path.join(__TEMP__,file_name), "rb") as f:
            while True:
                data = f.read(buf_size)
                if not data:
                    break
                self.write(data)
        
        allfiles = os.listdir(__TEMP__)
        for i in allfiles:
            if i.find(id) != -1:
                os.remove(os.path.join(__TEMP__,i))
        self.finish()



class CalculationDownloadPandInHandler(BaseHandler):
    """
    Handlers returning the downloaded data of a particular calculation task.
    Handlers returning the status of a particular calculation task.
    """
    def get(self, id):
        result = self._get_task_result(id)
  
        if self.request.connection.stream.closed():
            return
        file_name = "PandExo-Input-file"+id+".txt"
 
        #with open(os.path.join(__TEMP__,file_name), "w") as f:
        #    pickle.dump(result, f)
        np.savetxt(os.path.join(__TEMP__,file_name), np.transpose([result['w'], result['alpha']]))
        
        buf_size = 4096
        self.set_header('Content-Type', 'application/octet-stream')
        self.set_header('Content-Disposition',
                        'attachment; filename=' + file_name)
 
        with open(os.path.join(__TEMP__,file_name), "rb") as f:
            while True:
                data = f.read(buf_size)
                if not data:
                    break
                self.write(data)
        
        allfiles = os.listdir(__TEMP__)
        for i in allfiles:
            if i.find(id) != -1:
                os.remove(os.path.join(__TEMP__,i))


        self.finish()



class CalculationViewHandler(BaseHandler):
    """
    This handler deals with passing the results from Pandeia to the
    `create_component_jwst` function which generates the Bokeh interative plots.
    """
    def get(self, id):
        
        result = self._get_task_result(id)
        
        script, div = create_component_jwst(result)
        div['timing_div'] = result['timing_div']
        div['input_div'] = result['input_div'] 
        div['warnings_div'] = result['warnings_div']

        #delete files
        allfiles = os.listdir(__TEMP__)
        for i in allfiles:
            if i.find(id) != -1:
                os.remove(os.path.join(__TEMP__,i))

        self.render("view.html", script=script, div=div, id=id)



class CalculationViewHSTHandler(BaseHandler):
    """
    This handler deals with passing the results from Pandeia to the
    `create_component_hst` function which generates the Bokeh interative plots.
    """
    def get(self, id):
        result = self._get_task_result(id)
        script, div = create_component_hst(result)
        div['info_div'] = result['info_div']
        self.render("viewhst.html", script=script, div=div, id=id)


def main():
    tornado.options.parse_command_line()
    BaseHandler.executor = ProcessPoolExecutor(max_workers=options.workers)
    http_server = tornado.httpserver.HTTPServer(Application())
    http_server.listen(options.port)
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
