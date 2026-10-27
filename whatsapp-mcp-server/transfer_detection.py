"""Deterministic intake clues, never beneficiary validation or payment authority."""
import hashlib
import re
from decimal import Decimal, InvalidOperation

ACCOUNT = re.compile(r'(?<!\d)\d{10}(?!\d)')
SECURITY = re.compile(r'\b(?:otp|one[- ]time(?: password| code)?|verification code|login code|password|passcode|secret pin|do not share|authentication code)\b',re.I)
SYNTHETIC = re.compile(r'\b(?:synthetic|test only|sample account|example test bank|transfer_detector_test)\b',re.I)
RECEIPT = re.compile(r'\b(?:send (?:me )?(?:the )?receipt|proof of payment|payment (?:successful|received|completed)|transfer (?:successful|completed)|transaction reference|money (?:received|has arrived)|already paid)\b',re.I)
ACTION = re.compile(r'\b(?:please|pls|kindly|abeg|send|transfer|pay|borrow|lend|assist|help me|i need|we need|balance|settle|deposit|owe)\b',re.I)
MONEY = re.compile(r'\b(?:money|funds|naira|ngn|usd|eur|gbp|rent|loan|gas|school fees|bill|payment|acct|account|bank)\b|[₦$€£]',re.I)
BANK = re.compile(r'\b(?:bank|acct|account number|uba|gtbank|gtb|zenith|moniepoint|opay|palmpay|kuda|wema|alat|fcmb|fidelity|access bank|first bank|stanbic)\b',re.I)
AMOUNT = re.compile(r'(?:₦|\bNGN\s*|\bN(?=\s*\d))\s*(\d[\d,]*(?:\.\d{1,2})?)\s*(k|m|thousand|million)?\b|(?<!\d)(\d[\d,]*(?:\.\d{1,2})?)\s*(k|thousand|million|naira)\b',re.I)
BARE_AMOUNT = re.compile(r'\b(?:send|transfer|pay|need|borrow|lend|owe|balance|total)\s+(?:me\s+)?(\d[\d,]*(?:\.\d{1,2})?)\b',re.I)
FX_AMOUNT = re.compile(r'(USD|EUR|GBP|\$|€|£)\s*(\d[\d,]*(?:\.\d{1,2})?)',re.I)
IMPLICIT = re.compile(r'\b(?:gas|fuel|rent|school fees|electricity bill)\b.{0,50}\b(?:finish(?:ed)?|due|expired|renew)\b',re.I)
TEST_PREFIX = 'TRANSFER_DETECTOR_TEST:'

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
    context=[x for x in context if not sensitive(x.get('content','')) and not SYNTHETIC.search(x.get('content',''))]
    prior=[]
    for row in reversed(context):
        if row.get('id')==source.get('id'):continue
        body=row.get('content') or ''
        if RECEIPT.search(body):break
        prior.append(row)
    previous=next((x for x in prior if request_language(x.get('content',''))),None)
    accounts=list(dict.fromkeys(ACCOUNT.findall(text)))[:10]
    amounts=amount_clues(text)
    if previous and not amounts and re.fullmatch(r'\s*\d[\d,]*(?:\.\d{1,2})?\s*',text):
        value=text.strip().replace(',','')
        if len(value)<=20 and len(value.split('.')[0])!=10 and 0<Decimal(value)<=Decimal('1000000000000'):
            amounts=[{'text':text.strip(),'amount':format(Decimal(value),'f'),'currency_hint':None,'currency_explicit':False}]
    direct=request_language(text)
    reasons=[]
    if accounts and (BANK.search(text) or previous):reasons.append('payment_details_with_context')
    if direct:reasons.append('request_language_with_money_or_amount')
    if IMPLICIT.search(text):reasons.append('possible_bill_or_supply_request')
    if amounts and previous:reasons.append('amount_follow_up')
    if source.get('media_type') in ('image','document') and previous:reasons.append('attachment_in_request_context')
    if not reasons:return None
    anchor=source.get('id') if direct or not previous else previous['id']
    return {'classification':'heuristic_candidate','reasons':reasons,'account_number_candidates':accounts,
            'amount_candidates':amounts,'anchor_message_id':anchor,'beneficiary_verified':False,
            'amount_missing':not amounts and not any(amount_clues(x.get('content','')) for x in prior),
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
