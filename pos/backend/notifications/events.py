"""The things the POS tells people about.

Each event has a built-in default (who and which channels); Admin > Integrations
> Notifications can change the channels and add roles or named users on top.
An event appears there only once the code raises it, so the screen never offers
an alert that nothing sends.

`people` says who always hears about it, whatever the rule adds:
  approvers  the branch's approvers for the request (approvals.service)
  requester  the person who asked
  nobody     only the roles and users named in the rule
"""

EVENTS = {
    'approval.requested': {
        'label': 'An approval is needed',
        'help': 'A cashier asks for a price change or a discount above the free limit. '
                'Sent to the approvers set up for that branch, with Approve and Reject links.',
        'people': 'approvers', 'template': 'approval',
        'bell': True, 'email': True, 'whatsapp': True,
    },
    'approval.decided': {
        'label': 'An approval was decided',
        'help': 'Tells the person who asked that their request was approved or rejected.',
        'people': 'requester', 'template': 'general',
        'bell': True, 'email': True, 'whatsapp': False,
    },
    'stock.request_raised': {
        'label': 'A stock request was raised',
        'help': 'A branch asks for stock. The people who hold Approve Stock Requests for that '
                'branch are told automatically, with an Approve / Reject link in the e-mail '
                'and WhatsApp message; add anyone else here.',
        'people': 'nobody', 'template': 'approval',
        'bell': True, 'email': True, 'whatsapp': False,
    },
    'stock.transfer_sent': {
        'label': 'Stock is on its way to a branch',
        'help': 'Stock was sent. The people at the receiving branch who hold Stock Transfers are told '
                'automatically so they can count it and sign it in; add anyone else here.',
        'people': 'nobody', 'template': 'general',
        'bell': True, 'email': True, 'whatsapp': False,
    },
    'stock.transfer_received': {
        'label': 'A stock transfer was received',
        'help': 'The receiving branch signed the stock in. Whoever asked for it, approved it and sent it is '
                'told automatically, with a warning when less arrived than was sent; add anyone else here.',
        'people': 'nobody', 'template': 'general',
        'bell': True, 'email': True, 'whatsapp': False,
    },
    'stock.request_approved': {
        'label': 'A stock request was approved',
        'help': 'An approver agreed to a stock request. The people who hold Fulfill Stock Requests '
                'are told automatically so they can send it; add anyone else here.',
        'people': 'nobody', 'template': 'general',
        'bell': True, 'email': True, 'whatsapp': False,
    },
    'sap.posting_failed': {
        'label': 'A SAP posting needs attention',
        'help': 'A bill or stock move could not be posted to SAP after all retries. '
                'Add the people who look after SAP.',
        'people': 'nobody', 'template': 'general',
        'bell': True, 'email': True, 'whatsapp': False,
    },
    'closing.variance': {
        'label': 'A day-end closing is out of balance',
        'help': 'The cash counted differs from the cash the bills say should be there. '
                'Add the people who review closings.',
        'people': 'nobody', 'template': 'general',
        'bell': True, 'email': True, 'whatsapp': False,
    },
    'payroll.ready': {
        'label': 'Payroll is ready to review',
        'help': 'A month of payroll was generated as drafts. Add the people who lock it.',
        'people': 'nobody', 'template': 'general',
        'bell': True, 'email': False, 'whatsapp': False,
    },
    'inventory.low_stock': {
        'label': 'Stock is low',
        'help': 'A daily summary per branch of products at or below their reorder level.',
        'people': 'nobody', 'template': 'general',
        'bell': True, 'email': False, 'whatsapp': False,
    },
}

# Messages to clients (not users). Their channels follow the client's consent;
# these switches are the business's own on/off, in Admin > Settings.
CLIENT_EVENTS = {
    'client.expiry_reminder': 'Client reminder before a card, membership, package or cashback expires',
    'client.appointment_reminder': 'Client reminder before an appointment',
    'client.welcome': 'Welcome message to a new client',
    'client.campaign': 'Marketing campaign message',
}
