import os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import whatsapp

class DeviceContactSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'messages.db'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE chats(jid TEXT,name TEXT,last_message_time TEXT)')
            db.execute('INSERT INTO chats VALUES(?,?,?)',('99887766@lid','99887766',None))
        self.patches=[patch.object(whatsapp,'MESSAGES_DB_PATH',str(self.path)),
                      patch.dict(os.environ,{'WHATSAPP_BRIDGE_TOKEN':'synthetic-token'}),
                      patch.object(whatsapp,'search_macos_contacts',return_value=([],None))]
        for item in self.patches:item.start();self.addCleanup(item.stop)
    def test_saved_name_discovers_numeric_chat_and_verified_phone(self):
        entry={'jid':'15550000001@s.whatsapp.net','name':'Example Housekeeper','saved_name':'Example Housekeeper',
               'aliases':['15550000001@s.whatsapp.net','99887766@lid']}
        with patch.object(whatsapp,'_bridge_api_request',return_value={'success':True,'data':[entry]}) as bridge:
            result=whatsapp.search_contacts('Housekeeper')
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].phone_number,'15550000001');self.assertEqual(result[0].jid,'99887766@lid')
        self.assertTrue(result[0].has_whatsapp_chat);self.assertEqual(result[0].name,'Example Housekeeper')
        bridge.assert_called_once_with('GET','contacts/search',params={'query':'Housekeeper','limit':50})
    def test_lid_is_not_guessed_and_saved_name_wins_duplicate_profile(self):
        entries=[{'jid':'99887766@lid','name':'Profile Example','aliases':['99887766@lid']},
                 {'jid':'15550000002@s.whatsapp.net','name':'Saved Example','saved_name':'Saved Example','aliases':['15550000002@s.whatsapp.net']},
                 {'jid':'15550000002@s.whatsapp.net','name':'Profile Alias','aliases':['15550000002@s.whatsapp.net']}]
        with patch.object(whatsapp,'_bridge_api_request',return_value={'success':True,'data':entries}):
            result=whatsapp.search_contacts('Example')
        self.assertEqual(len(result),2)
        opaque=next(x for x in result if x.jid=='99887766@lid');self.assertIsNone(opaque.phone_number)
        saved=next(x for x in result if x.phone_number=='15550000002')
        self.assertEqual(saved.name,'Saved Example');self.assertFalse(saved.has_whatsapp_chat)
    def test_cached_chat_fallback_when_device_directory_is_unavailable(self):
        with patch.object(whatsapp,'_bridge_api_request',return_value={'success':False}):
            result=whatsapp.search_contacts('9988')
        self.assertEqual(len(result),1);self.assertEqual(result[0].jid,'99887766@lid');self.assertIsNone(result[0].phone_number)
