"""Deterministic intake clues, never beneficiary validation or payment authority."""
import hashlib
import re
from datetime import datetime,timedelta
from decimal import Decimal, InvalidOperation

ACCOUNT = re.compile(r'(?<!\d)(?:\d{10}|\d{3}[ -]\d{3}[ -]\d{4}|\d{4}[ -]\d{3}[ -]\d{3}|\d{5}[ -]\d{5})(?!\d)')
SECURITY = re.compile(r'\b(?:otp|one[- ]time(?: password| code)?|verification code|login code|access code|gate code|door code|password|passcode|secret pin|do not share|authentication code)\b',re.I)
SYNTHETIC = re.compile(r'\b(?:synthetic|test only|sample account|example test bank|transfer_detector_test)\b',re.I)
RECEIPT = re.compile(r'\b(?:send (?:me )?(?:the )?receipt|proof of payment|payment (?:successful|received|completed)|transfer (?:successful|completed)|transaction reference|money (?:received|has arrived)|already paid)\b',re.I)
ACTION = re.compile(r'\b(?:please|pls|kindly|abeg|send|transfer|pay|borrow|lend|assist|help me|i need|we need|balance|settle|deposit|owe)\b',re.I)
MONEY = re.compile(r'\b(?:money|funds|naira|ngn|usd|eur|gbp|rent|loan|gas|school fees|bill|payment|acct|account|bank)\b|[₦$€£]',re.I)
BANK = re.compile(r'\b(?:bank|acct|account number|uba|gtbank|gtb|gtco|guaranty trust|zenith|moniepoint|o\s*pay|palmpay|kuda|wema|alat|fcmb|fidelity|access|first\s*bank|stanbic|sterling|union bank|ecobank|polaris|keystone|providus|titan|optimus|globus|premium trust)\b',re.I)
AMOUNT = re.compile(r'(?:₦|\bNGN\s*|\bN(?=\s*\d))\s*(\d[\d,]*(?:\.\d{1,2})?)\s*(k|m|thousand|million)?\b|(?<!\d)(\d[\d,]*(?:\.\d{1,2})?)\s*(k|m|thousand|million|naira)\b',re.I)
BARE_AMOUNT = re.compile(r'\b(?:send|transfer|pay|need|borrow|lend|owe|balance|total)\s+(?:me\s+)?(\d[\d,]*(?:\.\d{1,2})?)\b',re.I)
FX_AMOUNT = re.compile(r'(USD|EUR|GBP|\$|€|£)\s*(\d[\d,]*(?:\.\d{1,2})?)',re.I)
IMPLICIT = re.compile(r'\b(?:gas|fuel|rent|school fees|electricity bill)\b.{0,50}\b(?:finish(?:ed)?|due|expired|renew)\b',re.I)
TEST_PREFIX = 'TRANSFER_DETECTOR_TEST:'
PURPOSE = re.compile(r'^(?:for\b|waybill\b|drinks\b|delivery\b|transport\b|gas\b|fuel\b|rent\b|school fees\b|electrician\b|plumbing\b|workmanship\b)',re.I)
TOTAL = re.compile(r'\b(?:total|balance|amount)\s*[:=\-]*\s*(\d[\d,]*(?:\.\d{1,2})?)\b',re.I)

def account_clues(text):
    return list(dict.fromkeys(re.sub(r'\D','',x) for x in ACCOUNT.findall(text)))[:10]

def recent_context(source,context,dry_run=False):
    """Only preceding same-chat evidence in a short request conversation window."""
    result=[]
    for row in context:
        if row.get('id')==source.get('id') or row.get('chat_jid')!=source.get('chat_jid'):continue
        try:
            age=datetime.fromisoformat(source['timestamp'].replace('Z','+00:00'))-datetime.fromisoformat(row['timestamp'].replace('Z','+00:00'))
            if not timedelta(0)<=age<=timedelta(hours=2):continue
        except (ValueError,TypeError,KeyError):continue
        body=row.get('content') or ''
        if dry_run:
            if not body.startswith(TEST_PREFIX):continue
            body=body[len(TEST_PREFIX):]
        if not sensitive(body) and not SYNTHETIC.search(body):result.append({**row,'content':body})
    return result

def amount_clues(text):
    result=[]
    for match in AMOUNT.finditer(text):
        try:
            number=Decimal((match.group(1) or match.group(3)).replace(',',''))
            suffix=(match.group(2) or match.group(4) or '').lower()
            number*= {'k':1000,'thousand':1000,'m':1000000,'million':1000000}.get(suffix,1)
            if number<=0 or number>Decimal('1000000000000'):continue
            result.append({'text':match.group(0).strip()[:100],'amount':format(number,'f'),
                           'currency_hint':'NGN','currency_explicit':bool(match.group(1) or suffix=='naira')})
        except InvalidOperation:continue
    for match in FX_AMOUNT.finditer(text):
        number=Decimal(match.group(2).replace(',',''))
        if 0<number<=Decimal('1000000000000'):
            currency={'$':'USD','€':'EUR','£':'GBP'}.get(match.group(1),match.group(1).upper())
            result.append({'text':match.group(0)[:100],'amount':format(number,'f'),'currency_hint':currency,'currency_explicit':True})
    for match in BARE_AMOUNT.finditer(text):
        value=match.group(1).replace(',','')
        if len(value.split('.')[0])==10:continue
        number=Decimal(value)
        if 0<number<=Decimal('1000000000000'):
            result.append({'text':match.group(1)[:100],'amount':format(number,'f'),'currency_hint':None,'currency_explicit':False})
    return result[:10]

def sensitive(text):
    return bool(SECURITY.search(text or ''))

def request_language(text):
    return bool(ACTION.search(text) and (MONEY.search(text) or amount_clues(text)))

def classify(source,context=(),dry_run=False):
    """Return evidence for a possible request; false positives need bot/owner review."""
    text=source.get('content') or ''
    if sensitive(text) or (SYNTHETIC.search(text) and not dry_run) or RECEIPT.search(text):return None
    if text.startswith(TEST_PREFIX):
        if not dry_run:return None
        text=text[len(TEST_PREFIX):]
    context=recent_context(source,context,dry_run)
    prior=[]
    for row in reversed(context):
        if row.get('id')==source.get('id'):continue
        body=row.get('content') or ''
        if RECEIPT.search(body):break
        prior.append(row)
    # Nearest request or adjacent account/bank pair, including split details.
    previous=next((x for i,x in enumerate(prior) if request_language(x.get('content','')) or
                   (account_clues(x.get('content','')) and (BANK.search(x.get('content','')) or
                    BANK.search(text) or any(BANK.search(y.get('content','')) for y in prior[max(0,i-2):i+3])))),None)
    accounts=account_clues(text)
    account_source=source.get('id')
    # A bank/name often follows the account number as a separate message.
    if not accounts and BANK.search(text) and len(text)<300:
        nearby=next((x for x in prior[:3] if account_clues(x.get('content',''))),None)
        if nearby:
            accounts=account_clues(nearby.get('content',''));account_source=nearby['id']
    amounts=amount_clues(text)
    if accounts and BANK.search(text):
        # Account/name/"1900" and itemized "total-48,500" are actual intake formats.
        for match in list(TOTAL.finditer(text))+list(re.finditer(r'(?<![\d,])(\d[\d,]*(?:\.\d{1,2})?)\s*$',text)):
            value=match.group(1).replace(',','')
            if re.match(r'\s*(?:k|m|thousand|million|naira)\b',text[match.end(1):],re.I):continue
            if len(value.split('.')[0])==10 or any(match.start(1)>=a.start() and match.end(1)<=a.end() for a in ACCOUNT.finditer(text)):continue
            if 0<Decimal(value)<=Decimal('1000000000000') and not any(x['amount']==format(Decimal(value),'f') for x in amounts):
                amounts.append({'text':match.group(0).strip()[:100],'amount':format(Decimal(value),'f'),'currency_hint':None,'currency_explicit':False})
    if previous and not amounts and re.fullmatch(r'\s*\d[\d,]*(?:\.\d{1,2})?\s*',text):
        value=text.strip().replace(',','')
        if len(value)<=20 and len(value.split('.')[0])!=10 and 0<Decimal(value)<=Decimal('1000000000000'):
            amounts=[{'text':text.strip(),'amount':format(Decimal(value),'f'),'currency_hint':None,'currency_explicit':False}]
    direct=request_language(text)
    reasons=[]
    if accounts and (BANK.search(text) or previous or any(BANK.search(x.get('content','')) for x in prior[:3])):reasons.append('payment_details_with_context')
    if direct:reasons.append('request_language_with_money_or_amount')
    if IMPLICIT.search(text):reasons.append('possible_bill_or_supply_request')
    if amounts and previous:reasons.append('amount_follow_up')
    purpose_follow_up=bool(previous and PURPOSE.search(text.strip()) and len(text)<400)
    if purpose_follow_up:reasons.append('purpose_follow_up')
    if source.get('media_type') in ('image','document') and previous:reasons.append('attachment_in_request_context')
    if not reasons:return None
    anchor=source.get('id') if direct or not previous else previous['id']
    previous_accounts=account_clues(previous.get('content','')) if previous else []
    if previous and previous_accounts and not request_language(previous.get('content','')) and not direct:
        # A split account/bank pair can itself follow the original request.
        # Walk back only until a different destination or the request anchor.
        for older in prior[prior.index(previous)+1:]:
            older_accounts=account_clues(older.get('content',''))
            if older_accounts and set(older_accounts)!=set(previous_accounts):break
            if request_language(older.get('content','')):
                anchor=older['id'];break
    different_destination=bool(accounts and previous_accounts and set(accounts)!=set(previous_accounts))
    if different_destination:anchor=account_source
    linked_prior=[]
    if anchor!=source.get('id'):
        for row in prior:
            linked_prior.append(row)
            if row['id']==anchor:break
    for amount in amounts:amount['source_message_id']=source.get('id')
    return {'classification':'heuristic_candidate','reasons':reasons,'account_number_candidates':accounts,
            'account_source_message_id':account_source if accounts else None,
            'amount_candidates':amounts,'anchor_message_id':anchor,'beneficiary_verified':False,
            'amount_ambiguous':len({(x['amount'],x['currency_hint']) for x in amounts})>1,
            'different_destination_from_previous':different_destination,
            'amount_missing':not amounts and not any(amount_clues(x.get('content','')) for x in linked_prior),
            'attachment_extraction_needed':source.get('media_type') in ('image','document')}

def digest(*values):
    return hashlib.sha256('\0'.join(str(x) for x in values).encode()).hexdigest()

def source_digest(source):
    return digest(source.get('content',''),source.get('media_type',''),source.get('filename',''),source.get('deleted',False))

def safe_context(rows,limit=12,max_chars=1600):
    clean=[];truncated=len(rows)>limit
    for row in rows[-limit:]:
        text=row.get('content') or ''
        if sensitive(text):continue
        if len(text)>max_chars:truncated=True
        clean.append({k:row.get(k) for k in ('id','chat_jid','timestamp','is_from_me','media_type','filename')}
                     | {'content':text[:max_chars]})
    return clean,truncated
