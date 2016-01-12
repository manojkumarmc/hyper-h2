# -*- coding: utf-8 -*-
"""
test_invalid_frame_sequences.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This module contains tests that use invalid frame sequences, and validates that
they fail appropriately.
"""
import pytest

import h2.connection
import h2.errors
import h2.exceptions


class TestInvalidFrameSequences(object):
    """
    Invalid frame sequences, either sent or received, cause ProtocolErrors to
    be thrown.
    """
    example_request_headers = [
        (':authority', 'example.com'),
        (':path', '/'),
        (':scheme', 'https'),
        (':method', 'GET'),
    ]
    example_response_headers = [
        (':status', '200'),
        ('server', 'fake-serv/0.1.0')
    ]

    def test_cannot_send_on_closed_stream(self):
        """
        When we've closed a stream locally, we cannot send further data.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers, end_stream=True)

        with pytest.raises(h2.exceptions.ProtocolError):
            c.send_data(1, b'some data')

    def test_missing_preamble_errors(self):
        """
        Server side connections require the preamble.
        """
        c = h2.connection.H2Connection(client_side=False)
        encoded_headers_frame = (
            b'\x00\x00\r\x01\x04\x00\x00\x00\x01'
            b'A\x88/\x91\xd3]\x05\\\x87\xa7\x84\x87\x82'
        )

        with pytest.raises(h2.exceptions.ProtocolError):
            c.receive_data(encoded_headers_frame)

    def test_server_connections_reject_even_streams(self, frame_factory):
        """
        Servers do not allow clients to initiate even-numbered streams.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.initiate_connection()
        c.receive_data(frame_factory.preamble())

        f = frame_factory.build_headers_frame(
            self.example_request_headers, stream_id=2
        )

        with pytest.raises(h2.exceptions.ProtocolError):
            c.receive_data(f.serialize())

    def test_clients_reject_odd_stream_pushes(self, frame_factory):
        """
        Clients do not allow servers to push odd numbered streams.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers, end_stream=True)

        f = frame_factory.build_push_promise_frame(
            stream_id=1,
            headers=self.example_request_headers,
            promised_stream_id=3
        )

        with pytest.raises(h2.exceptions.ProtocolError):
            c.receive_data(f.serialize())

    def test_can_handle_frames_with_invalid_padding(self, frame_factory):
        """
        Frames with invalid padding cause connection teardown.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.initiate_connection()
        c.receive_data(frame_factory.preamble())

        f = frame_factory.build_headers_frame(self.example_request_headers)
        c.receive_data(f.serialize())
        c.clear_outbound_data_buffer()

        invalid_data_frame = (
            b'\x00\x00\x05\x00\x0b\x00\x00\x00\x01\x06\x54\x65\x73\x74'
        )

        with pytest.raises(h2.exceptions.ProtocolError):
            c.receive_data(invalid_data_frame)

        expected_frame = frame_factory.build_goaway_frame(
            last_stream_id=1, error_code=1
        )
        assert c.data_to_send() == expected_frame.serialize()

    def test_reject_data_on_closed_streams(self, frame_factory):
        """
        When a stream is not open to the remote peer, we reject receiving data
        frames from them.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.initiate_connection()
        c.receive_data(frame_factory.preamble())

        f = frame_factory.build_headers_frame(
            self.example_request_headers,
            flags=['END_STREAM']
        )
        c.receive_data(f.serialize())
        c.clear_outbound_data_buffer()

        bad_frame = frame_factory.build_data_frame(data=b'hello')
        c.receive_data(bad_frame.serialize())

        expected_frame = frame_factory.build_rst_stream_frame(
            stream_id=1,
            error_code=0x5,
        )
        assert c.data_to_send() == expected_frame.serialize()

    def test_unexpected_continuation_on_closed_stream(self, frame_factory):
        """
        CONTINUATION frames received on closed streams cause stream errors of
        type STREAM_CLOSED.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.initiate_connection()
        c.receive_data(frame_factory.preamble())

        f = frame_factory.build_headers_frame(
            self.example_request_headers,
            flags=['END_STREAM']
        )
        c.receive_data(f.serialize())
        c.clear_outbound_data_buffer()

        bad_frame = frame_factory.build_continuation_frame(
            header_block=b'hello'
        )
        c.receive_data(bad_frame.serialize())

        expected_frame = frame_factory.build_rst_stream_frame(
            stream_id=1,
            error_code=0x5,
        )
        assert c.data_to_send() == expected_frame.serialize()

    # These settings are a bit annoyingly anonymous, but trust me, they're bad.
    @pytest.mark.parametrize(
        "settings",
        [
            {0x2: 5},
            {0x4: 2**31},
            {0x5: 5},
            {0x5: 2**24},
        ]
    )
    def test_reject_invalid_settings_values(self, frame_factory, settings):
        """
        When a SETTINGS frame is received with invalid settings values it
        causes connection teardown with the appropriate error code.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.initiate_connection()
        c.receive_data(frame_factory.preamble())

        f = frame_factory.build_settings_frame(settings=settings)

        with pytest.raises(h2.exceptions.InvalidSettingsValueError) as e:
            c.receive_data(f.serialize())

        assert e.value.error_code == (
            h2.errors.FLOW_CONTROL_ERROR if 0x4 in settings else
            h2.errors.PROTOCOL_ERROR
        )

    def test_invalid_frame_headers_are_protocol_errors(self, frame_factory):
        """
        When invalid frame headers are received they cause ProtocolErrors to be
        raised.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.initiate_connection()
        c.receive_data(frame_factory.preamble())

        f = frame_factory.build_headers_frame(
            headers=self.example_request_headers
        )

        # Do some annoying bit twiddling here: the stream ID is currently set
        # to '1', change it to '0'. Grab the first 9 bytes (the frame header),
        # replace any instances of the byte '\x01', and then graft it onto the
        # remaining bytes.
        frame_data = f.serialize()
        frame_data = frame_data[:9].replace(b'\x01', b'\x00') + frame_data[9:]

        with pytest.raises(h2.exceptions.ProtocolError) as e:
            c.receive_data(frame_data)

        assert "Stream ID must be non-zero" in str(e.value)
