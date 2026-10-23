from copy import deepcopy
import unittest
import test_unified_narration as fixtures
from app.unified_narration import _contains_private_endpoint

class DestinationPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.UnifiedNarrationTests('test_one_combined_payload_keeps_google_whole_and_provider_facts_separate')
        self.fixture.setUp()
        self.fixture.context.update(destination={'lat':51.140765,'lon':71.470765,'label':'PRIVATE_DEST_ADDRESS'},
                                    destination_source='google_places_text_search')

    def test_private_destination_injected_output_is_rejected_whole(self):
        answer=deepcopy(self.fixture.answer)
        answer['text']='PRIVATE_DEST_ADDRESS. '+answer['text']
        result,status=self.fixture.run_narrator(http_open=self.fixture.model(answer))
        self.assertIsNone(result)
        self.assertEqual(status,'sensitive_origin_in_model_output')
        self.assertEqual(len(self.fixture.calls),1)

    def test_private_destination_input_is_rejected_before_any_model_call(self):
        self.fixture.grounding['summary']+=' PRIVATE_DEST_ADDRESS'
        result,status=self.fixture.run_narrator()
        self.assertIsNone(result)
        self.assertEqual(status,'sensitive_origin_in_model_context')
        self.assertEqual(self.fixture.calls,[])

    def test_both_ends_coordinate_guard_and_public_endpoint_exemption(self):
        context=self.fixture.context
        self.assertTrue(_contains_private_endpoint('51.140765',context))
        self.assertTrue(_contains_private_endpoint('PRIVATE_START_ADDRESS',context))
        public={**context,'destination_source':'query_landmark'}
        self.assertFalse(_contains_private_endpoint('PRIVATE_DEST_ADDRESS',public))

if __name__=='__main__':unittest.main()
