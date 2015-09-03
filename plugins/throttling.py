# Author: Zhang Huangbin <zhb _at_ iredmail.org>
# Purpose: Throttle based on amount of mails and total mail size sent over
#          a given period of time, or size of single message.
#
# Note: To keep the database compact, you should set up a daily cron job to
#       clean up old/inactive records.

# Usage
# -------------
#
# *) Enable iRedAPD in Postfix parameters: `smtpd_recipient_restrictions` and
#    `smtpd_end_of_data_restrictions`. For example:
#
#    smtpd_recipient_restrictions =
#           ...
#           check_policy_service inet:[127.0.0.1]:7777
#           permit_mynetworks
#           ...
#
#    smtpd_end_of_data_restrictions =
#           check_policy_service inet:[127.0.0.1]:7777
#           ...
#
# *) Enable this plugin in iRedAPD config file /opt/iredapd/settings.py.
# *) Restart both iRedAPD and Postfix services.

# Technology details
# -------------
#
# Currently you may throttle based on amount of mails and total mail size
# sent over a given period of time, or size of singe message.
#
# Eg: You can enforce that user@domain.com does not send more than 1000 mails
# or 1GB of mail (whichever limit is hit first) in say a 5 minute period.
#
# Possible sender throttling methods:
#
# 1) Throttle by sender/recipient address (either SASL username or `From:`
#    address). Valid addresses are:
#
#       *) Full sender email address: user@domain.com
#       *) Domain name (with a prefixed '@'): @domain.com
#       *) Sub-domain name (with a prefixed '@.'): @.domain.com
#       *) IP address:  192.168.1.1
#       *) IP network:  192.168.1.*
#
#   Priorities (larger digital number has higher priority):
#
#       *) ip: 10,
#       *) email: 8,
#       *) wildcard_addr: 6,     # r'user@*'. used in iRedAPD plugin `amavisd_wblist`
#                                # as wildcard sender. e.g. 'user@*'
#       *) domain: 5,
#       *) subdomain: 3,
#       *) top_level_domain: 1,
#       *) catchall: 0,

# ------------
# Valid settings:
#
#   * msg_size: max size of single message
#   * max_msgs: max number of sent messages
#   * max_quota: max number of accumulated message size
#
# Sample sender throttle settings:
#
# *) Allow user `user@domain.com` to send in 6 minutes (period_sent=360):
#
#   * max 100 msgs (max_msg=100;)
#   * max 4096000000 bytes (max_quota=4096000000)
#   * max size of single message is 10240000 bytes (msg_size=10240000)
#
#  INSERT INTO throttle_sender (user, settings, period_sent, priority)
#                       VALUES ('user@domain.com',
#                               'max_msgs:100;max_quota:4096000000;msg_size:10240000;',
#                               360,
#                               10);
#
# Sample recipient throttle settings:
#
# *) Allow user 'user@domain.com' to receive in 6 minutes (period=360):
#
#    * max 100 msgs (max_msg=100;)
#    * max 4096000000 bytes (max_quota=4096000000)
#    * max size of single message is 10240000 bytes (msg_size=10240000)
#
#  INSERT INTO throttle_rcpt (user, settings, period_rcvd, priority)
#                     VALUES ('user@domain.com',
#                             'rcpt_max_msgs:100;rcpt_max_quota:4096000000;rcpt_msg_size:10240000;',
#                             360,
#                             10);
#
# ------------
# Possible value for throttle setting: msg_size, max_msgs, max_quota.
#
#  * XX (an integer number): explicit limit. e.g. 100. (max_msgs=100 means up to 100 messages)
#  * 0:  unlimited.
#  * -1: inherit setting which has lower priority

import time
import logging
from web import sqlliteral
import settings
from libs import SMTP_ACTIONS
from libs.utils import is_ipv4, wildcard_ipv4, sqllist, is_trusted_client
from libs.amavisd.core import get_valid_addresses_from_email

SMTP_PROTOCOL_STATE = ['RCPT', 'END-OF-MESSAGE']

# Connect to iredapd database
REQUIRE_IREDAPD_DB = True


def convert_throttle_setting_to_dict(s, value_is_integer=True):
    """Convert throttle setting string to dict.

    >>> convert_throttle_setting_to_dict('var:value;var2:value2;var3:vavlue3;')
    {'var': value,
     'var2': value2,
     'var3': value3}
    """

    if not s:
        return {}

    sd = {}

    # Get all single setting
    setting_items = [st for st in s.split(';') if ':' in st]
    for item in setting_items:
        if item:
            key, value = item.split(':')

            if value_is_integer:
                try:
                    value = int(value)
                except:
                    pass

            sd[key] = value

    return sd


# Apply throttle setting and return smtp action.
def apply_throttle(conn,
                   user,
                   client_address,
                   protocol_state,
                   size,
                   is_sender_throttling=True):
    possible_addrs = get_valid_addresses_from_email(user)
    possible_addrs.append(client_address)

    if is_ipv4(client_address):
        possible_addrs += wildcard_ipv4(client_address)

    sql_table = 'throttle_sender'
    if not is_sender_throttling:
        sql_table = 'throttle_rcpt'

    logging.debug('Possible addresses: %s' % str(possible_addrs))

    sql = """
        SELECT id, user, priority, period,
               max_msgs, max_quota, msg_size,
               cur_msgs, cur_quota, init_time, last_time
          FROM %s
         WHERE user IN %s
         ORDER BY priority DESC
         """ % (sql_table, sqllist(possible_addrs))

    logging.debug('[SQL] Query throttle setting: %s' % sql)
    qr = conn.execute(sql)
    sql_records = qr.fetchall()

    logging.debug('[SQL] Query result: %s' % str(sql_records))

    if not sql_records:
        logging.debug('No throttle setting.')
    else:
        # Don't check some throttle setting with lower priority.
        continue_check_msg_size = True
        continue_check_max_msgs = True
        continue_check_max_quota = True

        # If not rejected, update init_time, cur_msgs, cur_quota, last_time
        sql_update_sets = {}

        for rcd in sql_records:
            (t_id, t_user, priority, period,
             max_msgs, max_quota, msg_size,
             cur_msgs, cur_quota, init_time, last_time) = rcd

            # If no period, throttle setting is useless.
            if not period:
                logging.debug('No period, skip this setting.')
                return SMTP_ACTIONS['default']

            # Initialize with empty value
            sql_update_sets[t_id] = []

            if settings.log_level == 'debug':
                trtl = 'Apply throttle setting with priority %d:\n' % priority
                trtl += '         user: %s\n' % t_user
                trtl += '       period: %d (seconds)\n' % period
                trtl += '         ----\n'
                trtl += '     msg_size: %d (bytes. -1 means inherit setting with lower priority)\n' % msg_size
                trtl += '    max_quota: %d (bytes. -1 means inherit setting with lower priority)\n' % max_quota
                trtl += '     max_msgs: %d\n' % max_msgs
                trtl += '         ----\n'
                trtl += '     cur_msgs: %d\n' % cur_msgs
                trtl += '    cur_quota: %d (bytes)\n' % cur_quota
                trtl += '         ----\n'
                trtl += '    init_time: %d (seconds)\n' % init_time
                trtl += '    last_time: %d (seconds)' % last_time
                logging.debug(trtl)

            # Check `period`
            tracking_expired = False

            now = int(time.time())

            if now > (init_time + period):
                logging.debug('Throttle tracking expired, reset all tracking values.')
                tracking_expired = True

                # Reset current msgs and quota immediately.
                # Note: we reset `init_time` later in 'END-OF-MESSAGE'.
                cur_msgs = 0
                cur_quota = 0

            # Apply throttle setting on different protocol_state:
            #
            #   *           RCPT: max_msgs
            #   * END-OF-MESSAGE: msg_size, max_quota
            #
            # Note: Don't update `cur_msgs` in 'RCPT' state, because
            # current mail may be rejected by other plugins in 'END-OF-MESSAGE'
            # state or other restrictions in Postfix.
            if protocol_state == 'RCPT' and continue_check_max_msgs:
                if continue_check_max_msgs:
                    if max_msgs > 0:
                        if cur_msgs >= max_msgs:
                            logging.debug('Exceed max messages: cur_msgs (%d) >= max_msgs (%d).' % (cur_msgs, max_msgs))
                            return SMTP_ACTIONS['reject_exceed_max_msgs']
                        else:
                            logging.debug('Not exceed max messages: cur_msgs (%d) < max_msgs (%d).' % (cur_msgs, max_msgs))

                    # (max_msgs == -1): don't check throttl setting with lower priority
                    if max_msgs >= 0:
                        continue_check_max_msgs = False

            elif protocol_state == 'END-OF-MESSAGE' and (continue_check_msg_size or continue_check_max_quota):
                # Check message size
                if continue_check_msg_size:
                    if msg_size >= 0:
                        continue_check_msg_size = False

                    if msg_size > 0:
                        if size > msg_size:
                            logging.debug('Exceeded message size for single mail: max=%d bytes, current=%d bytes.' % (msg_size, size))
                            return SMTP_ACTIONS['reject_exceed_msg_size']
                        else:
                            # Update `total_msgs`
                            sql_update_sets[t_id].append('total_msgs = total_msgs + 1')

                # Check max quota
                if continue_check_max_quota:
                    if max_quota >= 0:
                        continue_check_max_quota = False

                    if max_quota > 0:
                        if cur_quota >= max_quota:
                            logging.debug('Exceeded accumulated message size: max=%d bytes, current=%d (bytes).' % (max_quota, cur_quota))
                            return SMTP_ACTIONS['reject_exceed_max_quota']
                        else:
                            sql_update_sets[t_id].append('total_quota = total_quota + %d' % size)

                if tracking_expired:
                    # Reset init_time, cur_msgs, max_quota
                    sql_update_sets[t_id].append('init_time = %d' % int(time.time()))

                    if max_msgs:
                        sql_update_sets[t_id].append('cur_msgs = 1')

                    if max_quota:
                        sql_update_sets[t_id].append('cur_quota = %d' % size)

                else:
                    if max_msgs:
                        sql_update_sets[t_id].append('cur_msgs = cur_msgs + 1')

                    if max_quota:
                        sql_update_sets[t_id].append('cur_quota = cur_quota + %d' % size)

                sql_update_sets[t_id].append('last_time = %d' % int(time.time()))

        # If not rejected, update init_time, cur_msgs, cur_quota, last_time
        if protocol_state == 'END-OF-MESSAGE' and sql_update_sets:
            for update_set in sql_update_sets:
                sql_update_set = ','.join(update_set)
                sql = """
                    UPDATE %s
                       SET %s
                     WHERE id=%d
                     """ % (sql_table, sqlliteral(sql_update_set), t_id)
                logging.debug('[SQL] Update throttle tracking: %s' % sql)
                conn.execute(sql)

    return SMTP_ACTIONS['default']


def restriction(**kwargs):
    conn = kwargs['conn_iredapd']

    sender = kwargs['sender']
    sender_domain = kwargs['sender_domain']
    recipient = kwargs['recipient']
    recipient_domain = kwargs['recipient_domain']
    client_address = kwargs['client_address']
    protocol_state = kwargs['smtp_session_data']['protocol_state']
    size = kwargs['smtp_session_data']['size']
    if size:
        size = int(size)
    else:
        size = 0

    if sender_domain == recipient_domain:
        logging.debug('Sender domain (@%s) is same as recipient domain, skip throttling.' % sender_domain)
        return SMTP_ACTIONS['default']

    if settings.THROTTLE_BYPASS_MYNETWORKS:
        if is_trusted_client(client_address):
            return SMTP_ACTIONS['default']

    logging.debug('Check sender throttling.')
    action = apply_throttle(conn=conn,
                            user=sender,
                            client_address=client_address,
                            protocol_state=protocol_state,
                            size=size,
                            is_sender_throttling=True)

    if not action.startswith('DUNNO'):
        return action

    logging.debug('Check recipient throttling.')
    action = apply_throttle(conn=conn,
                            user=recipient,
                            client_address=client_address,
                            protocol_state=protocol_state,
                            size=size,
                            is_sender_throttling=False)

    if not action.startswith('DUNNO'):
        return action

    return SMTP_ACTIONS['default']
