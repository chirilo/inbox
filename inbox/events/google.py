"""Provide Google Calendar events."""
import datetime
import urllib
import requests

from inbox.models import Event, Calendar, Account
from inbox.models.session import session_scope
from inbox.models.backends.oauth import token_manager
from inbox.events.util import parse_datetime

CALENDARS_URL = 'https://www.googleapis.com/calendar/v3/users/me/calendarList'
STATUS_MAP = {'accepted': 'yes', 'needsAction': 'noreply',
              'declined': 'no', 'tentative': 'maybe'}


class GoogleEventsProvider(object):
    """
    A utility class to fetch and parse Google calendar data for the
    specified account using the Google Calendar API.
    """

    def __init__(self, account_id, namespace_id):
        self.account_id = account_id
        self.namespace_id = namespace_id

    def get_calendars(self):
        deletes = []
        updates = []
        items = self._get_raw_calendars()
        for item in items:
            if item.get('deleted'):
                deletes.append(item['id'])
            else:
                updates.append(parse_calendar_response(item))

        return (deletes, updates)

    def get_events(self, calendar_uid, sync_from_time=None):
        """
        Fetch event data for an individual calendar.

        Parameters
        ----------
        calendar_uid: the google identifier for the calendar.
            Usually username@gmail.com for the primary calendar, otherwise
            random-alphanumeric-address@google.com
        sync_from_time: datetime
            Only sync events which have been added or changed since this time.
            Note that if this is too far in the past, the Google calendar API
            may return an HTTP 410 error, in which case we transparently fetch
            all event data.
        """
        deletes = []
        updates = []
        items = self._get_raw_events(calendar_uid, sync_from_time)
        for item in items:
            if item.get('status') == 'cancelled':
                deletes.append(item['id'])
            else:
                updates.append(parse_event_response(item))
        return (deletes, updates)

    def _get_raw_calendars(self):
        return self._get_resource_list(CALENDARS_URL)

    def _get_raw_events(self, calendar_uid, sync_from_time=None):
        if sync_from_time is not None:
            # Note explicit offset is required by Google calendar API.
            sync_from_time = datetime.datetime.isoformat(sync_from_time) + 'Z'

        url = 'https://www.googleapis.com/calendar/v3/' \
              'calendars/{}/events'.format(urllib.quote(calendar_uid))
        try:
            return self._get_resource_list(url, updatedMin=sync_from_time,
                                           singleEvents=True)
        except requests.exceptions.HTTPError as exc:
            if exc.response.status_code == 410:
                # The calendar API may return 410 if you pass a value for
                # updatedMin that's too far in the past. In that case, refetch
                # all events.
                return self._get_resource_list(url, singleEvents=True)
            else:
                raise

    def _get_access_token(self):
        with session_scope() as db_session:
            acc = db_session.query(Account).get(self.account_id)
            return token_manager.get_token(acc)

    def _get_resource_list(self, url, **params):
        token = self._get_access_token()
        items = []
        next_page_token = None
        params['showDeleted'] = True
        while True:
            if next_page_token is not None:
                params['pageToken'] = next_page_token
            r = requests.get(url, params=params, auth=OAuth(token))
            if r.status_code == 200:
                data = r.json()
                items += data['items']
                next_page_token = data.get('nextPageToken')
                if next_page_token is None:
                    return items
            elif r.status_code == 401:
                # get a new access token and retry
                # STOPSHIP(emfree): verify that this is right
                token = self._get_access_token()
            else:
                r.raise_for_status()

    def _make_event_request(self, method, calendar_uid, event_uid, **kwargs):
        event_uid = event_uid or ''
        url = 'https://www.googleapis.com/calendar/v3/' \
              'calendars/{}/events/{}'.format(urllib.quote(calendar_uid),
                                              urllib.quote(event_uid))
        token = self._get_access_token()
        r = requests.request(method, url, auth=OAuth(token), **kwargs)
        r.raise_for_status()
        return r

    def create_remote_event(self, event):
        data = _dump_event(event)
        r = self._make_event_request('post', event.calendar.uid, event.uid,
                                     json=data)
        return r.json()

    def update_remote_event(self, event):
        data = _dump_event(event)
        self._make_event_request('put', event.calendar.uid, event.uid,
                                 json=data)

    def delete_remote_event(self, calendar_uid, event_uid):
        self._make_event_request('delete', calendar_uid, event_uid)


def parse_calendar_response(calendar):
    """
    Constructs a Calendar object from a Google calendarList resource (a
    dictionary).  See
    https://developers.google.com/google-apps/calendar/v3/reference/calendarList

    Parameters
    ----------
    event: dict

    Returns
    -------
    A corresponding Event instance.
    """
    uid = calendar['id']
    name = calendar['summary']
    read_only = calendar['accessRole'] == 'reader'
    description = calendar.get('description', None)
    return Calendar(uid=uid,
                    name=name,
                    read_only=read_only,
                    description=description)


def parse_event_response(event):
    """
    Constructs an Event object from a Google event resource (a dictionary).
    See https://developers.google.com/google-apps/calendar/v3/reference/events

    Parameters
    ----------
    event: dict

    Returns
    -------
    A corresponding Event instance. This instance is not committed or added to
    a session.
    """
    uid = str(event['id'])
    # The entirety of the raw event data in json representation.
    raw_data = str(event)
    title = event.get('summary', '')

    # Timing data
    _start = event['start']
    _end = event['end']
    all_day = ('date' in _start and 'date' in _end)
    if all_day:
        start = parse_datetime(_start['date'])
        end = parse_datetime(_end['date']) - datetime.timedelta(days=1)
    else:
        start = parse_datetime(_start['dateTime'])
        end = parse_datetime(_end['dateTime'])

    description = event.get('description')
    location = event.get('location')
    busy = event.get('transparency') != 'transparent'

    # Ownership, read_only information
    creator = event.get('creator')

    if creator:
        owner = u'{} <{}>'.format(
            creator.get('displayName', ''), creator.get('email', ''))
    else:
        owner = ''

    is_owner = bool(creator and creator.get('self'))
    read_only = not (is_owner or event.get('guestsCanModify'))

    participants = []
    attendees = event.get('attendees', [])
    for attendee in attendees:
        status = STATUS_MAP[attendee.get('responseStatus')]
        participants.append({
            'email': attendee.get('email'),
            'name': attendee.get('displayName'),
            'status': status,
            'notes': attendee.get('comment')
        })

    return Event(uid=uid,
                 raw_data=raw_data,
                 title=title,
                 description=description,
                 location=location,
                 busy=busy,
                 start=start,
                 end=end,
                 all_day=all_day,
                 owner=owner,
                 read_only=read_only,
                 participants=participants,
                 # TODO(emfree): remove after data cleanup
                 source='local')


def _dump_event(event):
    """Convert an event db object to the Google API JSON format."""
    dump = {}
    dump["summary"] = event.title
    dump["description"] = event.description
    dump["location"] = event.location

    # Whether the event blocks time on the calendar.
    dump['transparency'] = 'opaque' if event.busy else 'transparent'

    if event.all_day:
        dump["start"] = {"date": event.start.strftime('%Y-%m-%d')}
    else:
        dump["start"] = {"dateTime": event.start.isoformat('T'),
                         "timeZone": "UTC"}
        dump["end"] = {"dateTime": event.end.isoformat('T'),
                       "timeZone": "UTC"}

    if event.participants:
        attendees = [_create_attendee(participant) for participant
                     in event.participants]
        dump["attendees"] = [attendee for attendee in attendees
                             if attendee]

    return dump


def _create_attendee(participant):
    inv_status_map = {value: key for key, value in STATUS_MAP.iteritems()}

    att = {}
    if 'name' in participant:
        att['displayName'] = participant['name']

    if 'status' in participant:
        att['responseStatus'] = inv_status_map[participant['status']]

    if 'email' in participant:
        att['email'] = participant['email']

    if 'guests' in participant:
        att['additionalGuests'] = participant['guests']

    return att


class OAuth(requests.auth.AuthBase):
    """Helper class for setting the Authorization header on HTTP requests."""
    def __init__(self, token):
        self.token = token

    def __call__(self, r):
        r.headers['Authorization'] = 'Bearer {}'.format(self.token)
        return r
