"""
When sending mail, Inbox tries to be a good citizen to the modern world.
This means everything we send is either ASCII or UTF-8.
That means no Latin-1 or ISO-8859-1.

All headers are converted to ASCII and if that doesn't work, UTF-8.

Note that plain text that's UTF-8 will be sent as base64. i.e.:
Content-Type: text/text; charset='utf-8'
Content-Transfer-Encoding: base64

This is because not all servers support 8BIT and so flanker drops to b64.
http://www.w3.org/Protocols/rfc1341/5_Content-Transfer-Encoding.html

"""
import pkg_resources

from flanker import mime
from flanker.addresslib import address
from html2text import html2text

from inbox.sqlalchemy_ext.util import generate_public_id

VERSION = pkg_resources.get_distribution('inbox-sync').version

REPLYSTR = 'Re: '


def create_email(sender_name,
                 sender_email,
                 inbox_uid,
                 to_addr,
                 cc_addr,
                 bcc_addr,
                 subject,
                 html,
                 in_reply_to,
                 references,
                 attachments):
    """
    Creates a MIME email message (both body and sets the needed headers).

    Parameters
    ----------
    sender_name: string
        The name aka phrase of the sender.
    sender_email: string
        The sender's email address.
    to_addr, cc_addr, bcc_addr: list of pairs (name, email_address), or None
        Message recipients.
    subject : string
        a utf-8 encoded string
    html : string
        a utf-8 encoded string
    in_reply_to: string or None
        If this message is a reply, the Message-Id of the message being replied
        to.
    references: list or None
        If this message is a reply, the Message-Ids of prior messages in the
        thread.
    attachments: list of dicts, optional
        a list of dicts(filename, data, content_type)
    """
    html = html if html else ''
    plaintext = html2text(html)

    # Create a multipart/alternative message
    msg = mime.create.multipart('alternative')
    msg.append(
        mime.create.text('plain', plaintext),
        mime.create.text('html', html))

    # Create an outer multipart/mixed message
    if attachments:
        text_msg = msg
        msg = mime.create.multipart('mixed')

        # The first part is the multipart/alternative text part
        msg.append(text_msg)

        # The subsequent parts are the attachment parts
        for a in attachments:
            # Disposition should be inline if we add Content-ID
            msg.append(mime.create.attachment(
                a['content_type'],
                a['data'],
                filename=a['filename'],
                disposition='attachment'))

    msg.headers['Subject'] = subject if subject else ''

    # Gmail sets the From: header to the default sending account. We can
    # however set our own custom phrase i.e. the name that appears next to the
    # email address (useful if the user has multiple aliases and wants to
    # specify which to send as), see: http://lee-phillips.org/gmailRewriting/
    # For other providers, we simply use name = ''
    from_addr = address.EmailAddress(sender_name, sender_email)
    msg.headers['From'] = from_addr.full_spec()

    # Need to set these headers so recipients know we sent the email to them
    # TODO(emfree): should these really be unicode?
    if to_addr:
        full_to_specs = [address.EmailAddress(name, spec).full_spec()
                         for name, spec in to_addr]
        msg.headers['To'] = u', '.join(full_to_specs)
    if cc_addr:
        full_cc_specs = [address.EmailAddress(name, spec).full_spec()
                         for name, spec in cc_addr]
        msg.headers['Cc'] = u', '.join(full_cc_specs)
    if bcc_addr:
        full_bcc_specs = [address.EmailAddress(name, spec).full_spec()
                          for name, spec in cc_addr]
        msg.headers['Bcc'] = u', '.join(full_bcc_specs)

    add_inbox_headers(msg, inbox_uid)

    if in_reply_to:
        msg.headers['In-Reply-To'] = in_reply_to
    if references:
        msg.headers['References'] = '\t'.join(references)

    rfcmsg = _rfc_transform(msg)

    return rfcmsg


def add_inbox_headers(msg, inbox_uid):
    """
    Set a custom `X-INBOX-ID` header so as to identify messages generated by
    Inbox.

    The header is set to a unique id generated randomly per message,
    and is needed for the correct reconciliation of sent messages on
    future syncs.

    Notes
    -----
    We generate the UUID as a base-36 encoded string, and is the same as the
    public_id of the message object.

    """
    # Set our own custom header for tracking in `Sent Mail` folder
    msg.headers['X-INBOX-ID'] = inbox_uid if inbox_uid else \
        generate_public_id()  # base-36 encoded string

    # Potentially also use `X-Mailer`
    msg.headers['User-Agent'] = 'Inbox/{0}'.format(VERSION)


def _rfc_transform(msg):
    """ Create an RFC-2821 compliant SMTP message.
    (Specifically, this means splitting the References header to conform to
    line length limits.)

    TODO(emfree): should we split recipient headers too?
    (The answer is probably yes)
    """
    msgstring = msg.to_string()

    start = msgstring.find('References: ')

    if start == -1:
        return msgstring

    end = msgstring.find('\r\n', start + len('References: '))

    substring = msgstring[start:end]

    separator = '\n\t'
    rfcmsg = msgstring[:start] + substring.replace('\t', separator) +\
        msgstring[end:]

    return rfcmsg
