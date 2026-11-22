#!/usr/bin/env python3
"""Default is a plan only. --send requires separate, explicit live-test approval."""
import argparse
import asyncio
import json
import os
import sys
import uuid

PLAN = """Live latency test plan (nothing sent):
1. After Blessing approves deployment, deploy the same reviewed commit to both accounts.
2. Configure the receiver to upsert by request_key, dedupe event_id, handle withdrawals,
   and record card_created/owner_alerted using record_intake_stage.
3. With separate approval, supply owner OAuth access tokens through explicit environment
   overrides WHATSAPP_E2E_SECONDARY_TOKEN and WHATSAPP_E2E_MAIN_TOKEN.
4. Run with --send, --secondary-mcp-url, --main-mcp-url and --main-recipient.
   Only synthetic TRANSFER_DETECTOR_TEST: evidence is sent secondary to main.
5. Read get_transfer_latency_report, match the source event IDs and verify owner-stage
   message_to_owner p90/max below 5000 ms. HTTP acceptance is not owner-card proof.
No deployment, restart, push notification or payment is performed by this script."""


async def live(args):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    secondary = os.environ.get('WHATSAPP_E2E_SECONDARY_TOKEN')
    main = os.environ.get('WHATSAPP_E2E_MAIN_TOKEN')
    if not all((secondary,main,args.secondary_mcp_url,args.main_mcp_url,args.main_recipient)):
        raise ValueError('Explicit endpoints, recipient and owner token overrides required')
    from urllib.parse import urlsplit
    for url in (args.secondary_mcp_url,args.main_mcp_url):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Credential-free HTTPS MCP endpoints required')
    text = 'TRANSFER_DETECTOR_TEST: Please send NGN 500 for plumbing. 09100000000 OPay Demo Beneficiary'
    async with streamablehttp_client(args.secondary_mcp_url,headers={'Authorization':'Bearer '+secondary}) as (read,write,_):
        async with ClientSession(read,write) as session:
            await session.initialize()
            sent = await session.call_tool('send_message', {'recipient':args.main_recipient,
                'message':text,'request_id':'latency-test-'+uuid.uuid4().hex})
            if sent.isError:
                raise ValueError('Synthetic test dispatch failed; no automatic resend')
            data=sent.structuredContent or json.loads(sent.content[0].text)
            data=data.get('result',data)
            if not data.get('success'):
                raise ValueError('Synthetic test dispatch not accepted; no automatic resend')
    await asyncio.sleep(5)
    async with streamablehttp_client(args.main_mcp_url,headers={'Authorization':'Bearer '+main}) as (read,write,_):
        async with ClientSession(read,write) as session:
            await session.initialize()
            report=await session.call_tool('get_transfer_latency_report',{'days':1})
            if report.isError:
                raise ValueError('Latency report unavailable')
            data=report.structuredContent or json.loads(report.content[0].text)
            # The report contains measurements/event IDs only; never print the send result.
            print(json.dumps(data))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send',action='store_true')
    parser.add_argument('--secondary-mcp-url')
    parser.add_argument('--main-mcp-url')
    parser.add_argument('--main-recipient')
    args=parser.parse_args()
    if not args.send:
        print(PLAN)
        return 0
    try:
        asyncio.run(live(args))
        return 0
    except Exception:
        # Service errors may contain credentials, message text or phone numbers.
        print('Live test failed; inspect content-free service status. No automatic retry.',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
