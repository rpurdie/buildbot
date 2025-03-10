# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members


from unittest import mock

from twisted.internet import defer
from twisted.trial import unittest

from buildbot.data import masters
from buildbot.db.masters import MasterModel
from buildbot.process.results import RETRY
from buildbot.test import fakedb
from buildbot.test.fake import fakemaster
from buildbot.test.reactor import TestReactorMixin
from buildbot.test.util import endpoint
from buildbot.test.util import interfaces
from buildbot.util import epoch2datetime

SOMETIME = 1349016870
OTHERTIME = 1249016870


class MasterEndpoint(endpoint.EndpointMixin, unittest.TestCase):
    endpointClass = masters.MasterEndpoint
    resourceTypeClass = masters.Master

    @defer.inlineCallbacks
    def setUp(self):
        yield self.setUpEndpoint()
        self.master.name = "myname"
        yield self.master.db.insert_test_data([
            fakedb.Master(id=13, active=False, last_active=SOMETIME),
            fakedb.Master(id=14, active=False, last_active=SOMETIME),
            fakedb.Builder(id=23, name='bldr1'),
            fakedb.BuilderMaster(builderid=23, masterid=13),
            fakedb.Builder(id=24, name='bldr2'),
        ])

    @defer.inlineCallbacks
    def test_get_existing(self):
        master = yield self.callGet(('masters', 14))

        self.validateData(master)
        self.assertEqual(master['name'], 'master-14')

    @defer.inlineCallbacks
    def test_get_builderid_existing(self):
        master = yield self.callGet(('builders', 23, 'masters', 13))

        self.validateData(master)
        self.assertEqual(master['name'], 'master-13')

    @defer.inlineCallbacks
    def test_get_builderid_no_match(self):
        master = yield self.callGet(('builders', 24, 'masters', 13))

        self.assertEqual(master, None)

    @defer.inlineCallbacks
    def test_get_builderid_missing(self):
        master = yield self.callGet(('builders', 25, 'masters', 13))

        self.assertEqual(master, None)

    @defer.inlineCallbacks
    def test_get_missing(self):
        master = yield self.callGet(('masters', 99))

        self.assertEqual(master, None)


class MastersEndpoint(endpoint.EndpointMixin, unittest.TestCase):
    endpointClass = masters.MastersEndpoint
    resourceTypeClass = masters.Master

    @defer.inlineCallbacks
    def setUp(self):
        yield self.setUpEndpoint()
        self.master.name = "myname"
        yield self.master.db.insert_test_data([
            fakedb.Master(id=13, active=False, last_active=SOMETIME),
            fakedb.Master(id=14, active=True, last_active=OTHERTIME),
            fakedb.Builder(id=22),
            fakedb.BuilderMaster(masterid=13, builderid=22),
        ])

    @defer.inlineCallbacks
    def test_get(self):
        masters = yield self.callGet(('masters',))

        for m in masters:
            self.validateData(m)

        self.assertEqual(sorted([m['masterid'] for m in masters]), [13, 14])

    @defer.inlineCallbacks
    def test_get_builderid(self):
        masters = yield self.callGet(('builders', 22, 'masters'))

        for m in masters:
            self.validateData(m)

        self.assertEqual(sorted([m['masterid'] for m in masters]), [13])

    @defer.inlineCallbacks
    def test_get_builderid_missing(self):
        masters = yield self.callGet(('builders', 23, 'masters'))

        self.assertEqual(masters, [])


class Master(TestReactorMixin, interfaces.InterfaceTests, unittest.TestCase):
    @defer.inlineCallbacks
    def setUp(self):
        self.setup_test_reactor()
        self.master = yield fakemaster.make_master(self, wantMq=True, wantDb=True, wantData=True)
        self.rtype = masters.Master(self.master)

    def test_signature_masterActive(self):
        @self.assertArgSpecMatches(
            self.master.data.updates.masterActive,  # fake
            self.rtype.masterActive,
        )  # real
        def masterActive(self, name, masterid):
            pass

    @defer.inlineCallbacks
    def test_masterActive(self):
        self.reactor.advance(60)

        yield self.master.db.insert_test_data([
            fakedb.Master(id=13, active=0, last_active=0),
            fakedb.Master(id=14, active=1, last_active=0),
            fakedb.Master(id=15, active=1, last_active=0),
        ])

        # initial checkin
        yield self.rtype.masterActive(name='master-13', masterid=13)
        master = yield self.master.db.masters.getMaster(13)
        self.assertEqual(
            master,
            MasterModel(id=13, name='master-13', active=True, last_active=epoch2datetime(60)),
        )
        self.assertEqual(
            self.master.mq.productions,
            [
                (
                    ('masters', '13', 'started'),
                    {"masterid": 13, "name": 'master-13', "active": True},
                ),
            ],
        )
        self.master.mq.productions = []

        # updated checkin time, re-activation
        self.reactor.advance(60)
        yield self.master.db.masters.setMasterState(13, False)
        yield self.rtype.masterActive('master-13', masterid=13)
        master = yield self.master.db.masters.getMaster(13)
        self.assertEqual(
            master,
            MasterModel(id=13, name='master-13', active=True, last_active=epoch2datetime(120)),
        )
        self.assertEqual(
            self.master.mq.productions,
            [
                (
                    ('masters', '13', 'started'),
                    {"masterid": 13, "name": 'master-13', "active": True},
                ),
            ],
        )
        self.master.mq.productions = []

    def test_signature_masterStopped(self):
        @self.assertArgSpecMatches(
            self.master.data.updates.masterStopped,  # fake
            self.rtype.masterStopped,
        )  # real
        def masterStopped(self, name, masterid):
            pass

    @defer.inlineCallbacks
    def test_masterStopped(self):
        self.reactor.advance(60)

        yield self.master.db.insert_test_data([
            fakedb.Master(id=13, name='aname', active=1, last_active=self.reactor.seconds()),
        ])

        self.rtype._masterDeactivated = mock.Mock()
        yield self.rtype.masterStopped(name='aname', masterid=13)
        self.rtype._masterDeactivated.assert_called_with(13, 'aname')

    @defer.inlineCallbacks
    def test_masterStopped_already(self):
        self.reactor.advance(60)

        yield self.master.db.insert_test_data([
            fakedb.Master(id=13, name='aname', active=0, last_active=0),
        ])

        self.rtype._masterDeactivated = mock.Mock()
        yield self.rtype.masterStopped(name='aname', masterid=13)
        self.rtype._masterDeactivated.assert_not_called()

    def test_signature_expireMasters(self):
        @self.assertArgSpecMatches(
            self.master.data.updates.expireMasters,  # fake
            self.rtype.expireMasters,
        )  # real
        def expireMasters(self, forceHouseKeeping=False):
            pass

    @defer.inlineCallbacks
    def test_expireMasters(self):
        self.reactor.advance(60)

        yield self.master.db.insert_test_data([
            fakedb.Master(id=14, active=1, last_active=0),
            fakedb.Master(id=15, active=1, last_active=0),
        ])

        self.rtype._masterDeactivated = mock.Mock()

        # check after 10 minutes, and see #14 deactivated; #15 gets deactivated
        # by another master, so it's not included here
        self.reactor.advance(600)
        yield self.master.db.masters.setMasterState(15, False)
        yield self.rtype.expireMasters()
        master = yield self.master.db.masters.getMaster(14)
        self.assertEqual(
            master,
            MasterModel(id=14, name='master-14', active=False, last_active=epoch2datetime(0)),
        )
        self.rtype._masterDeactivated.assert_called_with(14, 'master-14')

    @defer.inlineCallbacks
    def test_masterDeactivated(self):
        yield self.master.db.insert_test_data([
            fakedb.Master(id=14, name='other', active=0, last_active=0),
            # set up a running build with some steps
            fakedb.Builder(id=77, name='b1'),
            fakedb.Worker(id=13, name='wrk'),
            fakedb.Buildset(id=8822),
            fakedb.BuildRequest(id=82, builderid=77, buildsetid=8822),
            fakedb.BuildRequestClaim(brid=82, masterid=14, claimed_at=SOMETIME),
            fakedb.Build(
                id=13,
                builderid=77,
                masterid=14,
                workerid=13,
                buildrequestid=82,
                number=3,
                results=None,
            ),
            fakedb.Step(id=200, buildid=13),
            fakedb.Log(id=2000, stepid=200, num_lines=2),
            fakedb.LogChunk(logid=2000, first_line=1, last_line=2, content='ab\ncd'),
        ])

        # mock out the _masterDeactivated methods this will call
        for rtype in 'builder', 'scheduler', 'changesource':
            rtype_obj = getattr(self.master.data.rtypes, rtype)
            m = mock.Mock(name=f'{rtype}._masterDeactivated', spec=rtype_obj._masterDeactivated)
            m.side_effect = lambda masterid: defer.succeed(None)
            rtype_obj._masterDeactivated = m

        # and the update methods..
        for meth in 'finishBuild', 'finishStep', 'finishLog':
            m = mock.create_autospec(getattr(self.master.data.updates, meth))
            m.side_effect = lambda *args, **kwargs: defer.succeed(None)
            setattr(self.master.data.updates, meth, m)

        yield self.rtype._masterDeactivated(14, 'other')

        self.master.data.rtypes.builder._masterDeactivated.assert_called_with(masterid=14)
        self.master.data.rtypes.scheduler._masterDeactivated.assert_called_with(masterid=14)
        self.master.data.rtypes.changesource._masterDeactivated.assert_called_with(masterid=14)

        # see that we finished off that build and its steps and logs
        updates = self.master.data.updates
        updates.finishLog.assert_called_with(logid=2000)
        updates.finishStep.assert_called_with(stepid=200, results=RETRY, hidden=False)
        updates.finishBuild.assert_called_with(buildid=13, results=RETRY)

        self.assertEqual(
            self.master.mq.productions,
            [
                (('masters', '14', 'stopped'), {"masterid": 14, "name": 'other', "active": False}),
            ],
        )
