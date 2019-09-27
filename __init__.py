# Copyright 2017, Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import re
from time import sleep

import holidays
import pytz
from adapt.intent import IntentBuilder
from astral import Astral

import mycroft.audio
from mycroft import MycroftSkill, intent_handler, intent_file_handler
from mycroft.api import GeolocationApi
from mycroft.messagebus.message import Message
from mycroft.skills.core import resting_screen_handler
from mycroft.util.format import nice_date, nice_duration, nice_time
from mycroft.util.parse import (
    extract_datetime,
    fuzzy_match,
    extract_number,
    normalize
)
from mycroft.util.time import now_utc, to_local

MARK_1_NUMBER_WIDTH = 4  # digits are 3 pixels wide + a space
MARK_1_COLON_WIDTH = 2  # colon is 1 pixel wide + a space
MARK_1_DISPLAY_WIDTH = 32

mark_1_display_codes = {
    ':': 'CIICAA',
    '0': 'EIMHEEMHAA',
    '1': 'EIIEMHAEAA',
    '2': 'EIEHEFMFAA',
    '3': 'EIEFEFMHAA',
    '4': 'EIMBABMHAA',
    '5': 'EIMFEFEHAA',
    '6': 'EIMHEFEHAA',
    '7': 'EIEAEAMHAA',
    '8': 'EIMHEFMHAA',
    '9': 'EIMBEBMHAA',
    '9x8_blank': 'JIAAAAAAAAAAAAAAAAAA',
    '7x8_blank': 'HIAAAAAAAAAAAAAAAAAA',
    'alarm_dot': 'CIAACA',
    'no_alarm_dot': 'CIAAAA'
}


class DateTimeSkill(MycroftSkill):

    def __init__(self):
        super(DateTimeSkill, self).__init__("DateTimeSkill")
        self.astral = Astral()
        self.displayed_time = None
        self.display_tz = None
        self.answering_query = False
        self.geolocation_api = GeolocationApi()

    def initialize(self):
        # Start a callback that repeats every 10 seconds
        # TODO: Add mechanism to only start timer when UI setting
        #       is checked, but this requires a notifier for settings
        #       updates from the web.
        now = datetime.datetime.now()
        callback_time = (datetime.datetime(now.year, now.month, now.day,
                                           now.hour, now.minute) +
                         datetime.timedelta(seconds=60))
        self.schedule_repeating_event(self.update_display, callback_time, 10)

    # TODO:19.08 Moved to MycroftSkill
    @property
    def platform(self):
        """ Get the platform identifier string

        Returns:
            str: Platform identifier, such as "mycroft_mark_1",
                 "mycroft_picroft", "mycroft_mark_2".  None for nonstandard.
        """
        if self.config_core and self.config_core.get("enclosure"):
            return self.config_core["enclosure"].get("platform")
        else:
            return None

    @resting_screen_handler('Time and Date')
    def handle_idle(self, message):
        self.gui.clear()
        self.log.debug('Activating Time/Date resting page')
        self.gui['time_string'] = self.get_display_current_time()
        self.gui['ampm_string'] = ''
        self.gui['date_string'] = self.get_display_date()
        self.gui['weekday_string'] = self.get_weekday()
        self.gui['month_string'] = self.get_month_date()
        self.gui['year_string'] = self.get_year()
        self.gui.show_page('idle.qml')

    @property
    def time_format_24_hour(self):
        return self.config_core.get('time_format') == 'full'

    @property
    def display_is_idle(self):
        """Boolean indicating if the display is being used by another skill."""
        return self.enclosure.display_manager.get_active() == ''

    @property
    def alarm_is_set(self):
        msg = self.bus.wait_for_response(Message("private.mycroftai.has_alarm"))
        return msg and msg.data.get("active_alarms", 0) > 0

    # Deprecate
    def get_timezone(self, locale):
        try:
            # This handles common city names, like "Dallas" or "Paris"
            return pytz.timezone(self.astral[locale].timezone)
        except:
            pass

        try:
            # This handles codes like "America/Los_Angeles"
            return pytz.timezone(locale)
        except:
            pass

        # Check lookup table for other timezones.  This can also
        # be a translation layer.
        # E.g. "china = GMT+8"
        timezones = self.translate_namedvalues("timezone.value")
        for timezone in timezones:
            if locale.lower() == timezone.lower():
                # assumes translation is correct
                return pytz.timezone(timezones[timezone].strip())

        # Now we gotta get a little fuzzy
        # Look at the pytz list of all timezones. It consists of
        # Location/Name pairs.  For example:
        # ["Africa/Abidjan", "Africa/Accra", ... "America/Denver", ...
        #  "America/New_York", ..., "America/North_Dakota/Center", ...
        #  "Cuba", ..., "EST", ..., "Egypt", ..., "Etc/GMT+3", ...
        #  "Etc/Zulu", ... "US/Eastern", ... "UTC", ..., "Zulu"]
        target = locale.lower()
        best = None
        for name in pytz.all_timezones:
            normalized = name.lower().replace("_", " ").split("/") # E.g. "Australia/Sydney"
            if len(normalized) == 1:
                pct = fuzzy_match(normalized[0], target)
            elif len(normalized) >= 2:
                pct = fuzzy_match(normalized[1], target)                           # e.g. "Sydney"
                pct2 = fuzzy_match(normalized[-2] + " " + normalized[-1], target)  # e.g. "Sydney Australia" or "Center North Dakota"
                pct3 = fuzzy_match(normalized[-1] + " " + normalized[-2], target)  # e.g. "Australia Sydney"
                pct = max(pct, pct2, pct3)
            if not best or pct >= best[0]:
                best = (pct, name)
        if best and best[0] > 0.8:
           # solid choice
           return pytz.timezone(best[1])
        if best and best[0] > 0.3:
            # Convert to a better speakable version
            say = re.sub(r"([a-z])([A-Z])", r"\g<1> \g<2>", best[1])  # e.g. EasterIsland  to "Easter Island"
            say = say.replace("_", " ")  # e.g. "North_Dakota" to "North Dakota"
            say = say.split("/")  # e.g. "America/North Dakota/Center" to ["America", "North Dakota", "Center"]
            say.reverse()
            say = " ".join(say)   # e.g.  "Center North Dakota America", or "Easter Island Chile"
            if self.ask_yesno("did.you.mean.timezone", data={"zone_name": say}) == "yes":
                return pytz.timezone(best[1])

        return None

    # Deprecate
    def get_local_datetime(self, location, dtUTC=None):
        if not dtUTC:
            dtUTC = now_utc()
        if self.display_tz:
            # User requested times be shown in some timezone
            tz = self.display_tz
        else:
            tz = self.get_timezone(self.location_timezone)

        if location:
            tz = self.get_timezone(location)
        if not tz:
            self.speak_dialog("time.tz.not.found", {"location": location})
            return None

        return dtUTC.astimezone(tz)

    # Deprecate
    def get_display_date(self, day=None, location=None):
        if not day:
            day = self.get_local_datetime(location)
        if self.config_core.get('date_format') == 'MDY':
            return day.strftime("%-m/%-d/%Y")
        else:
            return day.strftime("%Y/%-d/%-m")

    # Deprecate
    def get_display_current_time(self, location=None, dtUTC=None):
        # Get a formatted digital clock time based on the user preferences
        dt = self.get_local_datetime(location, dtUTC)
        if not dt:
            return None

        return nice_time(dt, self.lang, speech=False,
                         use_24hour=self.time_format_24_hour)

    def display_gui(self, display_time):
        """ Display time on the Mycroft GUI. """
        self.gui.clear()
        self.gui['time_string'] = display_time
        self.gui['ampm_string'] = ''
        self.gui['date_string'] = self.get_display_date()
        self.gui.show_page('time.qml')

    def update_display(self, force=False):
        """Display the time if the display is not in use.

        The display is considered "in use" if another query in this skill is
        actively using the display or the enclosure reports that the display
        is in use for another reason.
        """
        while self.answering_query:
            sleep(1)

        current_datetime = datetime.datetime.now(self.display_tz)
        display_time = self._get_display_time(current_datetime)
        self.gui['time_string'] = display_time
        self.gui['date_string'] = self.get_display_date()
        self.gui['ampm_string'] = ''  # TODO

        if self.settings.get("show_time", False) or force:
            if self.display_is_idle:
                if self.displayed_time != display_time:
                    self.displayed_time = display_time
                    self._send_time_to_display(display_time)
                    self.enclosure.display_manager.remove_active()
            else:
                self.displayed_time = None  # another skill is using display
        else:
            if self.displayed_time is not None:
                if self.display_is_idle:
                    self.enclosure.mouth_reset()
                    self.enclosure.display_manager.remove_active()
                self.displayed_time = None

    @intent_handler(IntentBuilder('').require('Query').require('Time').
                    optionally('Location'))
    def handle_query_time(self, message):
        """Handle a request for the current time."""
        utterance = message.data.get('utterance')
        location = self._extract_location(utterance) if utterance else None
        tz = self._get_timezone(location)
        if tz is not None:
            current_datetime = datetime.datetime.now(tz)
            use_am_pm = location is not None
            worded_time = self._get_worded_time(current_datetime, use_am_pm)
            self.speak_dialog("time.current", {"time": worded_time})
            self._show_time(current_datetime)
            mycroft.audio.wait_while_speaking()
            self._reset_display()

    @intent_handler(IntentBuilder('current_time_handler_simple').
                    require('Time').optionally('Location'))
    def handle_current_time_simple(self, message):
        self.handle_query_time(message)

    @intent_file_handler('what.time.will.it.be.intent')
    def handle_query_future_time(self, message):
        utterance = message.data.get('utterance')
        utterance_datetime, utterance = self._parse_future_time_utterance(
            utterance
        )
        if utterance is not None:
            location = self._extract_location(utterance)
            tz = self._get_timezone(location)
            if tz is not None:
                future_datetime = utterance_datetime.astimezone(tz)
                worded_time = self._get_worded_time(
                    future_datetime,
                    use_am_pm=True
                )
                self.speak_dialog('time.future', {'time': worded_time})
                self._show_time(future_datetime)
                mycroft.audio.wait_while_speaking()
                self._reset_display()

    @intent_handler(IntentBuilder('future_time_handler_simple').
                    require('Time').require('Future').optionally('Location'))
    def handle_future_time_simple(self, message):
        self.handle_query_future_time(message)

    @intent_handler(IntentBuilder('').require('Display').require('Time').
                    optionally('Location'))
    def handle_show_time(self, message):
        self.display_tz = None
        utterance = message.data.get('utterance')
        location = self._extract_location(utterance) if utterance else None
        tz = self._get_timezone(location)
        if tz is not None:
            self.display_tz = tz
            self.update_display(force=True)

    def handle_query_date(self, message, response_type="simple"):
        utt = message.data.get('utterance', "").lower()
        try:
            extract = extract_datetime(utt)
        except:
            self.speak_dialog('date.not.found')
            return
        day = extract[0]

        # check if a Holiday was requested, e.g. "What day is Christmas?"
        year = extract_number(utt)
        if not year or year < 1500 or year > 3000:  # filter out non-years
            year = day.year
        all = {}
        # TODO: How to pick a location for holidays?
        for st in holidays.US.STATES:
            l = holidays.US(years=[year], state=st)
            for d, name in l.items():
                if not name in all:
                    all[name] = d
        for name in all:
            d = all[name]
            # Uncomment to display all holidays in the database
            # self.log.info("Day, name: " +str(d) + " " + str(name))
            if name.replace(" Day", "").lower() in utt:
                day = d
                break

        location = self._extract_location(utt)
        today = to_local(now_utc())
        if location:
            # TODO: Timezone math!
            if (day.year == today.year and day.month == today.month
                and day.day == today.day):
                day = now_utc()  # for questions ~ "what is the day in sydney"
            day = self.get_local_datetime(location, dtUTC=day)
        if not day:
            return  # failed in timezone lookup

        speak_date = nice_date(day, lang=self.lang)
        # speak it
        if response_type is "simple":
            self.speak_dialog("date", {"date": speak_date})
        elif response_type is "relative":
            # remove time data to get clean dates
            day_date = day.replace(hour=0, minute=0,
                                   second=0, microsecond=0)
            today_date = today.replace(hour=0, minute=0,
                                       second=0, microsecond=0)
            num_days = (day_date - today_date).days
            if num_days >= 0:
                speak_num_days = nice_duration(num_days * 86400)
                self.speak_dialog("date.relative.future",
                                  {"date": speak_date,
                                   "num_days": speak_num_days})
            else:
                # if in the past, make positive before getting duration
                speak_num_days = nice_duration(num_days * -86400)
                self.speak_dialog("date.relative.past",
                                  {"date": speak_date,
                                   "num_days": speak_num_days})

        # and briefly show the date
        self.answering_query = True
        self.show_date(location, day=day)
        sleep(10)
        mycroft.audio.wait_while_speaking()
        if self.platform == "mycroft_mark_1":
            self.enclosure.mouth_reset()
            self.enclosure.activate_mouth_events()
        self.answering_query = False
        self.displayed_time = None

    @intent_handler(IntentBuilder("").require("Query").require("Date").
                    optionally("Location"))
    def handle_query_date_simple(self, message):
        self.handle_query_date(message, response_type="simple")

    @intent_handler(IntentBuilder("").require("Query").require("Month"))
    def handle_day_for_date(self, message):
        self.handle_query_date(message, response_type="relative")

    @intent_handler(IntentBuilder("").require("Query").require("RelativeDay")
                                     .optionally("Date"))
    def handle_query_relative_date(self, message):
        self.handle_query_date(message, response_type="relative")

    @intent_handler(IntentBuilder("").require("RelativeDay").require("Date"))
    def handle_query_relative_date_alt(self, message):
        self.handle_query_date(message, response_type="relative")

    @intent_file_handler("date.future.weekend.intent")
    def handle_date_future_weekend(self, message):
        # Strip year off nice_date as request is inherently close
        # Don't pass `now` to `nice_date` as a
        # request on Friday will return "tomorrow"
        saturday_date = ', '.join(nice_date(extract_datetime(
                        'this saturday')[0]).split(', ')[:2])
        sunday_date = ', '.join(nice_date(extract_datetime(
                      'this sunday')[0]).split(', ')[:2])
        self.speak_dialog('date.future.weekend', {
            'direction': 'next',
            'saturday_date': saturday_date,
            'sunday_date': sunday_date
        })

    @intent_file_handler("date.last.weekend.intent")
    def handle_date_last_weekend(self, message):
        # Strip year off nice_date as request is inherently close
        # Don't pass `now` to `nice_date` as a
        # request on Monday will return "yesterday"
        saturday_date = ', '.join(nice_date(extract_datetime(
                        'this saturday')[0]).split(', ')[:2])
        sunday_date = ', '.join(nice_date(extract_datetime(
                      'this sunday')[0]).split(', ')[:2])
        self.speak_dialog('date.last.weekend', {
            'direction': 'last',
            'saturday_date': saturday_date,
            'sunday_date': sunday_date
        })

    @intent_handler(IntentBuilder("").require("Query").require("LeapYear"))
    def handle_query_next_leap_year(self, message):
        now = datetime.datetime.now()
        leap_date = datetime.datetime(now.year, 2, 28)
        year = now.year if now <= leap_date else now.year + 1
        next_leap_year = self.get_next_leap_year(year)
        self.speak_dialog('next.leap.year', {'year': next_leap_year})

    def _extract_location(self, utterance):
        """Extract the location from the utterance."""
        location = None
        for regex_pattern in self._get_location_patterns():
            search_result = re.search(regex_pattern, utterance)
            if search_result:
                try:
                    location = search_result.group("Location")
                except IndexError:
                    pass
                else:
                    self.log.debug('Found location in utterance: ' + location)
                    break

        return location

    def _get_location_patterns(self):
        """Get the regular expressions for finding location in an utterance.

        The regular expression used can differ by language so search the
        regex directory in this skill's root directory for a file that
        matches the user's configured language.
        """
        location_patterns = []
        regex_file_path = self.find_resource('location.rx', 'regex')
        if regex_file_path:
            with open(regex_file_path) as regex_file:
                for record in regex_file.readlines():
                    record_is_comment = record.startswith("#")
                    if not record_is_comment:
                        location_patterns.append(record.strip())

        return location_patterns

    def _get_timezone(self, location):
        try:
            if location is None:
                tz_code = self.config_core['location']['timezone']['code']
            else:
                geolocation = self.geolocation_api.get_geolocation(location)
                log_msg = 'Geolocation for "{}" is: {}'
                self.log.info(log_msg.format(location, geolocation))
                tz_code = geolocation['timezone']
        except KeyError:
            tz = None
        else:
            try:
                tz = pytz.timezone(tz_code)
            except pytz.exceptions.UnknownTimeZoneError:
                tz = None

        if tz is None:
            self.speak_dialog("time.tz.not.found", {"location": location})

        return tz

    def _get_worded_time(self, response_datetime, use_am_pm):
        """Convert datetime object to words based on the user preferences."""
        time_dialog = nice_time(
            response_datetime,
            self.lang,
            speech=True,
            use_24hour=self.time_format_24_hour,
            use_ampm=use_am_pm
        )
        # HACK: Mimic 2 has a bug with saying "AM".  Work around it for now.
        if use_am_pm:
            time_dialog = time_dialog.replace("AM", "A.M.")

        return time_dialog

    def _get_display_time(self, response_datetime):
        # Get a formatted digital clock time based on the user preferences
        return nice_time(
            response_datetime,
            self.lang,
            speech=False,
            use_24hour=self.time_format_24_hour
        )

    def _parse_future_time_utterance(self, utterance):
        utterance_datetime = remaining_utterance = None
        if utterance is not None:
            utterance = normalize(utterance.lower())
            parsed_utterance = extract_datetime(utterance)
            if parsed_utterance:
                utterance_datetime, remaining_utterance = parsed_utterance
            else:
                self.log.error(
                    'Failed to extract a datetime from utterance: ' + utterance
                )
                self.speak_dialog("skill.error")

        return utterance_datetime, remaining_utterance

    def _show_time(self, response_datetime):
        """Briefly show the time on the display, if there is one."""
        self.answering_query = True
        self.enclosure.deactivate_mouth_events()
        display_time = self._get_display_time(response_datetime)
        self._send_time_to_display(display_time)
        sleep(5)

    def _send_time_to_display(self, display_time):
        if display_time:
            if self.platform == "mycroft_mark_1":
                self._display_time_on_mark_1(display_time)
            self.display_gui(display_time)

    def _display_time_on_mark_1(self, display_time):
        self._clear_mark_1_display(display_time)
        self._center_time_on_mark_1_display(display_time)
        self._show_mark_1_alarm_indicator()

    def _clear_mark_1_display(self, display_time):
        """Draw two blank sections on Mark I display, numbers cover the rest"""
        if len(display_time) == 4:
            # for 4-character times (e.g. '1:00'), 9x8 blank on each side
            image_code = mark_1_display_codes['9x8_blank']
            x_offset = 22
        else:
            # for 5-character times (e.g. '12:00'), 7x8 blank on each side
            image_code = mark_1_display_codes['7x8_blank']
            x_offset = 24

        self.enclosure.mouth_display(image_code, refresh=False)
        self.enclosure.mouth_display(image_code, x=x_offset, refresh=False)

    def _center_time_on_mark_1_display(self, display_time):
        """Map characters to the display encoding for a Mark 1"""
        time_display_width = MARK_1_NUMBER_WIDTH * len(display_time)
        time_display_width -= MARK_1_COLON_WIDTH
        x_offset = (MARK_1_DISPLAY_WIDTH - time_display_width) / 2
        for character in display_time:
            if character in mark_1_display_codes:
                self.enclosure.mouth_display(
                    img_code=mark_1_display_codes[character],
                    x=x_offset,
                    refresh=False
                )
                if character == ':':
                    x_offset += MARK_1_COLON_WIDTH
                else:
                    x_offset += MARK_1_NUMBER_WIDTH

    def _show_mark_1_alarm_indicator(self):
        """Show a dot in the upper-left if an alarm is set."""
        if self.alarm_is_set:
            upper_left_code = mark_1_display_codes['alarm_dot']
        else:
            upper_left_code = mark_1_display_codes['no_alarm_dot']
        self.enclosure.mouth_display(upper_left_code, x=29, refresh=False)

    def _reset_display(self):
        """Reset the display after showing the date or time."""
        self.enclosure.mouth_reset()
        self.enclosure.activate_mouth_events()
        self.answering_query = False
        self.displayed_time = None

    def show_date(self, location, day=None):
        if self.platform == "mycroft_mark_1":
            self.show_date_mark1(location, day)
        self.show_date_gui(location, day)

    def show_date_mark1(self, location, day):
        show = self.get_display_date(day, location)
        self.enclosure.deactivate_mouth_events()
        self.enclosure.mouth_text(show)

    def get_weekday(self, day=None, location=None):
        if not day:
            day = self.get_local_datetime(location)
        return day.strftime("%A")

    def get_month_date(self, day=None, location=None):
        if not day:
            day = self.get_local_datetime(location)
        return day.strftime("%B %d")

    def get_year(self, day=None, location=None):
        if not day:
            day = self.get_local_datetime(location)
        return day.strftime("%Y")

    def get_next_leap_year(self, year):
        next_year = year + 1
        if self.is_leap_year(next_year):
            return next_year
        else:
            return self.get_next_leap_year(next_year)

    def is_leap_year(self, year):
        return (year % 400 == 0) or ((year % 4 == 0) and (year % 100 != 0))

    def show_date_gui(self, location, day):
        self.gui.clear()
        self.gui['date_string'] = self.get_display_date(day, location)
        self.gui['weekday_string'] = self.get_weekday(day, location)
        self.gui['month_string'] = self.get_month_date(day, location)
        self.gui['year_string'] = self.get_year(day, location)
        self.gui.show_page('date.qml')


def create_skill():
    return DateTimeSkill()
