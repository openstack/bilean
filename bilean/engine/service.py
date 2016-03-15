#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import functools
import six
import socket

from oslo_log import log as logging
import oslo_messaging
from oslo_service import service

from bilean.common import context as bilean_context
from bilean.common import exception
from bilean.common.i18n import _
from bilean.common.i18n import _LE
from bilean.common.i18n import _LI
from bilean.common import messaging as rpc_messaging
from bilean.common import schema
from bilean.common import utils
from bilean.engine import environment
from bilean.engine import event as event_mod
from bilean.engine import policy as policy_mod
from bilean.engine import resource as resource_mod
from bilean.engine import scheduler
from bilean.engine import user as user_mod
from bilean.rules import base as rule_base

LOG = logging.getLogger(__name__)


def request_context(func):
    @functools.wraps(func)
    def wrapped(self, ctx, *args, **kwargs):
        if ctx is not None and not isinstance(ctx,
                                              bilean_context.RequestContext):
            ctx = bilean_context.RequestContext.from_dict(ctx.to_dict())
        try:
            return func(self, ctx, *args, **kwargs)
        except exception.BileanException:
            raise oslo_messaging.rpc.dispatcher.ExpectedException()
    return wrapped


class EngineService(service.Service):
    """Manages the running instances from creation to destruction.

    All the methods in here are called from the RPC backend.  This is
    all done dynamically so if a call is made via RPC that does not
    have a corresponding method here, an exception will be thrown when
    it attempts to call into this class.  Arguments to these methods
    are also dynamically added and will be named as keyword arguments
    by the RPC caller.
    """

    RPC_API_VERSION = '1.1'

    def __init__(self, host, topic, manager=None, context=None):
        super(EngineService, self).__init__()
        self.host = host
        self.topic = topic

        self.scheduler = None
        self.engine_id = None
        self.target = None
        self._rpc_server = None

        if context is None:
            self.context = bilean_context.get_admin_context()

    def start(self):
        self.engine_id = socket.gethostname()

        LOG.info(_LI("Initialise bilean users from keystone."))
        user_mod.User.init_users(self.context)

        self.scheduler = scheduler.BileanScheduler(engine_id=self.engine_id,
                                                   context=self.context)
        LOG.info(_LI("Starting billing scheduler for engine: %s"),
                 self.engine_id)
        self.scheduler.init_scheduler()
        self.scheduler.start()

        LOG.info(_LI("Starting rpc server for engine: %s"), self.engine_id)
        target = oslo_messaging.Target(version=self.RPC_API_VERSION,
                                       server=self.host,
                                       topic=self.topic)
        self.target = target
        self._rpc_server = rpc_messaging.get_rpc_server(target, self)
        self._rpc_server.start()

        super(EngineService, self).start()

    def _stop_rpc_server(self):
        # Stop RPC connection to prevent new requests
        LOG.info(_LI("Stopping engine service..."))
        try:
            self._rpc_server.stop()
            self._rpc_server.wait()
            LOG.info(_LI('Engine service stopped successfully'))
        except Exception as ex:
            LOG.error(_LE('Failed to stop engine service: %s'),
                      six.text_type(ex))

    def stop(self):
        self._stop_rpc_server()

        LOG.info(_LI("Stopping billing scheduler for engine: %s"),
                 self.engine_id)
        self.scheduler.stop()

        super(EngineService, self).stop()

    @request_context
    def user_list(self, cnxt, show_deleted=False, limit=None,
                  marker=None, sort_keys=None, sort_dir=None,
                  filters=None):
        limit = utils.parse_int_param('limit', limit)
        show_deleted = utils.parse_bool_param('show_deleted', show_deleted)

        users = user_mod.User.load_all(cnxt, show_deleted=show_deleted,
                                       limit=limit, marker=marker,
                                       sort_keys=sort_keys, sort_dir=sort_dir,
                                       filters=filters)

        return [user.to_dict() for user in users]

    def user_create(self, cnxt, user_id, balance=None, credit=None,
                    status=None):
        """Create a new user from notification."""
        user = user_mod.User(user_id, balance=balance, credit=credit,
                             status=status)
        user.store(cnxt)

        return user.to_dict()

    @request_context
    def user_get(self, cnxt, user_id):
        """Show detailed info about a specify user.

        Realtime balance would be return.
        """
        user = user_mod.User.load(cnxt, user_id=user_id, realtime=True)
        return user.to_dict()

    @request_context
    def user_recharge(self, cnxt, user_id, value):
        """Do recharge for specify user."""
        user = user_mod.User.load(cnxt, user_id=user_id)
        user.do_recharge(cnxt, value)
        # As user has been updated, the billing job for the user
        # should to be updated too.
        self.scheduler.update_user_job(user)
        return user.to_dict()

    def user_delete(self, cnxt, user_id):
        """Delete a specify user according to the notification."""
        LOG.info(_LI('Deleging user: %s'), user_id)
        user = user_mod.User.load(cnxt, user_id=user_id)
        if user.status in [user.ACTIVE, user.WARNING]:
            LOG.error(_LE("User (%s) is in use, can not delete."), user_id)
            return
        user_mod.User.delete(cnxt, user_id=user_id)
        self.scheduler.delete_user_jobs(user)

    @request_context
    def user_attach_policy(self, cnxt, user_id, policy_id):
        """Attach specified policy to user."""
        LOG.info(_LI("Attaching policy %(policy)s to user %(user)s."),
                 {'policy': policy_id, 'user': user_id})
        user = user_mod.User.load(cnxt, user_id=user_id)
        if user.policy_id is not None:
            msg = _("User %(user)s is using policy %(now_policy)s, can not "
                    "attach %(policy)s.") % {'user': user_id,
                                             'now_policy': user.policy_id,
                                             'policy': policy_id}
            raise exception.BileanBadRequest(msg=msg)

        user.policy_id = policy_id
        user.store(cnxt)
        return user.to_dict()

    @request_context
    def rule_create(self, cnxt, name, spec, metadata=None):
        if len(rule_base.Rule.load_all(cnxt, filters={'name': name})) > 0:
            msg = _("The rule (%(name)s) already exists."
                    ) % {"name": name}
            raise exception.BileanBadRequest(msg=msg)

        type_name, version = schema.get_spec_version(spec)
        try:
            plugin = environment.global_env().get_rule(type_name)
        except exception.RuleTypeNotFound:
            msg = _("The specified rule type (%(type)s) is not supported."
                    ) % {"type": type_name}
            raise exception.BileanBadRequest(msg=msg)

        LOG.info(_LI("Creating rule type: %(type)s, name: %(name)s."),
                 {'type': type_name, 'name': name})
        rule = plugin(name, spec, metadata=metadata)
        try:
            rule.validate()
        except exception.InvalidSpec as ex:
            msg = six.text_type(ex)
            LOG.error(_LE("Failed in creating rule: %s"), msg)
            raise exception.BileanBadRequest(msg=msg)

        rule.store(cnxt)
        LOG.info(_LI("Rule %(name)s is created: %(id)s."),
                 {'name': name, 'id': rule.id})
        return rule.to_dict()

    @request_context
    def rule_list(self, cnxt, limit=None, marker=None, sort_keys=None,
                  sort_dir=None, filters=None, show_deleted=False):
        if limit is not None:
            limit = utils.parse_int_param('limit', limit)
        if show_deleted is not None:
            show_deleted = utils.parse_bool_param('show_deleted',
                                                  show_deleted)
        rules = rule_base.Rule.load_all(cnxt, limit=limit,
                                        marker=marker,
                                        sort_keys=sort_keys,
                                        sort_dir=sort_dir,
                                        filters=filters,
                                        show_deleted=show_deleted)

        return [rule.to_dict() for rule in rules]

    @request_context
    def rule_get(self, cnxt, rule_id):
        rule = rule_base.Rule.load(cnxt, rule_id=rule_id)
        return rule.to_dict()

    @request_context
    def rule_update(self, cnxt, rule_id, values):
        return NotImplemented

    @request_context
    def rule_delete(self, cnxt, rule_id):
        LOG.info(_LI("Deleting rule: '%s'."), rule_id)
        rule_base.Rule.delete(cnxt, rule_id)

    @request_context
    def validate_creation(self, cnxt, resources):
        """Validate resources creation.

        If user's balance is not enough for resources to keep 1 hour,
        will fail to validate.
        """
        user = user_mod.User.load(cnxt, user_id=cnxt.project)
        policy = policy_mod.Policy.load(cnxt, policy_id=user.policy_id)
        count = resources.get('count', 1)
        total_rate = 0
        for resource in resources['resources']:
            rule = policy.find_rule(cnxt, resource['resource_type'])
            res = resource_mod.Resource('FAKE_ID', user.id,
                                        resource['resource_type'],
                                        resource['properties'])
            total_rate += rule.get_price(res)
        if count > 1:
            total_rate = total_rate * count
        # Pre 1 hour bill for resources
        pre_bill = total_rate * 3600
        if pre_bill > user.balance:
            return dict(validation=False)
        return dict(validation=True)

    def resource_create(self, cnxt, resource_id, user_id, resource_type,
                        properties):
        """Create resource by given database

        Cause new resource would update user's rate, user update and billing
        would be done.

        """
        resource = resource_mod.Resource(resource_id, user_id, resource_type,
                                         properties)
        # Find the exact rule of resource
        user = user_mod.User.load(self.context, user_id=user_id)
        user_policy = policy_mod.Policy.load(
            self.context, policy_id=user.policy_id)
        rule = user_policy.find_rule(self.context, resource_type)

        # Update resource with rule_id and rate
        resource.rule_id = rule.id
        resource.rate = rule.get_price(resource)

        # Update user with resource
        user.update_with_resource(self.context, resource)
        resource.store(self.context)

        # As the rate of user has changed, the billing job for the user
        # should change too.
        self.scheduler.update_user_job(user)

        return resource.to_dict()

    @request_context
    def resource_list(self, cnxt, user_id=None, limit=None, marker=None,
                      sort_keys=None, sort_dir=None, filters=None,
                      project_safe=True, show_deleted=False):
        if limit is not None:
            limit = utils.parse_int_param('limit', limit)
        if show_deleted is not None:
            show_deleted = utils.parse_bool_param('show_deleted',
                                                  show_deleted)
        resources = resource_mod.Resource.load_all(cnxt, user_id=user_id,
                                                   limit=limit, marker=marker,
                                                   sort_keys=sort_keys,
                                                   sort_dir=sort_dir,
                                                   filters=filters,
                                                   project_safe=project_safe,
                                                   show_deleted=show_deleted)
        return [r.to_dict() for r in resources]

    @request_context
    def resource_get(self, cnxt, resource_id):
        resource = resource_mod.Resource.load(cnxt, resource_id=resource_id)
        return resource.to_dict()

    def resource_update(self, cnxt, resource):
        """Do resource update."""
        res = resource_mod.Resource.load(
            self.context, resource_id=resource['id'])
        old_rate = res.rate
        res.properties = resource['properties']
        rule = rule_base.Rule.load(self.context, rule_id=res.rule_id)
        res.rate = rule.get_price(res)
        res.store(self.context)
        res.d_rate = res.rate - old_rate

        user = user_mod.User.load(self.context, res.user_id)
        user.update_with_resource(self.context, res, action='update')

        self.scheduler.update_user_job(user)

    def resource_delete(self, cnxt, resource_id):
        """Do resource delete"""
        res = resource_mod.Resource.load(
            self.context, resource_id=resource_id, project_safe=False)
        user = user_mod.User.load(self.context, user_id=res.user_id)
        user.update_with_resource(self.context, res, action='delete')
        self.scheduler.update_user_job(user)
        try:
            res.do_delete(self.context)
        except Exception as ex:
            LOG.warn(_("Delete resource error %s"), ex)
            return

    @request_context
    def event_list(self, cnxt, user_id=None, limit=None, marker=None,
                   sort_keys=None, sort_dir=None, filters=None,
                   start_time=None, end_time=None, project_safe=True,
                   show_deleted=False):
        if limit is not None:
            limit = utils.parse_int_param('limit', limit)
        if show_deleted is not None:
            show_deleted = utils.parse_bool_param('show_deleted',
                                                  show_deleted)

        events = event_mod.Event.load_all(cnxt, user_id=user_id,
                                          limit=limit, marker=marker,
                                          sort_keys=sort_keys,
                                          sort_dir=sort_dir,
                                          filters=filters,
                                          start_time=start_time,
                                          end_time=end_time,
                                          project_safe=project_safe,
                                          show_deleted=show_deleted)
        return [e.to_dict() for e in events]

    @request_context
    def policy_create(self, cnxt, name, rule_ids=None, metadata=None):
        """Create a new policy."""
        if len(policy_mod.Policy.load_all(cnxt, filters={'name': name})) > 0:
            msg = _("The policy (%(name)s) already exists."
                    ) % {"name": name}
            raise exception.BileanBadRequest(msg=msg)

        rules = []
        if rule_ids is not None:
            type_cache = []
            for rule_id in rule_ids:
                try:
                    rule = rule_base.Rule.load(cnxt, rule_id=rule_id)
                    if rule.type not in type_cache:
                        rules.append({'id': rule_id, 'type': rule.type})
                        type_cache.append(rule.type)
                    else:
                        msg = _("More than one rule in type: '%s', it's "
                                "not allowed.") % rule.type
                        raise exception.BileanBadRequest(msg=msg)
                except exception.RuleNotFound as ex:
                    raise exception.BileanBadRequest(msg=six.text_type(ex))

        kwargs = {
            'rules': rules,
            'metadata': metadata,
        }
        policy = policy_mod.Policy(name, **kwargs)
        policy.store(cnxt)
        LOG.info(_LI("Policy is created: %(id)s."), policy.id)
        return policy.to_dict()

    @request_context
    def policy_list(self, cnxt, limit=None, marker=None, sort_keys=None,
                    sort_dir=None, filters=None, show_deleted=False):
        if limit is not None:
            limit = utils.parse_int_param('limit', limit)
        if show_deleted is not None:
            show_deleted = utils.parse_bool_param('show_deleted',
                                                  show_deleted)
        policies = policy_mod.Policy.load_all(cnxt, limit=limit,
                                              marker=marker,
                                              sort_keys=sort_keys,
                                              sort_dir=sort_dir,
                                              filters=filters,
                                              show_deleted=show_deleted)

        return [policy.to_dict() for policy in policies]

    @request_context
    def policy_get(self, cnxt, policy_id):
        policy = policy_mod.Policy.load(cnxt, policy_id=policy_id)
        return policy.to_dict()

    @request_context
    def policy_update(self, cnxt, policy_id, name=None, metadata=None,
                      is_default=None):
        LOG.info(_LI("Updating policy: '%(id)s'"), {'id': policy_id})

        policy = policy_mod.Policy.load(cnxt, policy_id=policy_id)
        changed = False
        if name is not None and name != policy.name:
            policies = policy_mod.Policy.load_all(cnxt, filters={'name': name})
            if len(policies) > 0:
                msg = _("The policy (%(name)s) already exists."
                        ) % {"name": name}
                raise exception.BileanBadRequest(msg=msg)
            policy.name = name
            changed = True
        if metadata is not None and metadata != policy.metadata:
            policy.metadata = metadata
            changed = True
        if is_default is not None and is_default != policy.is_default:
            is_default = utils.parse_bool_param('is_default', is_default)
            if is_default:
                # Set policy to default should unset old default policy.
                policies = policy_mod.load_all(cnxt,
                                               filters={'is_default': True})
                if len(policies) == 1:
                    default_policy = policies[0]
                    default_policy.is_default = False
                    default_policy.store(cnxt)
            policy.is_default = is_default
            changed = True

        if changed:
            policy.store(cnxt)

        LOG.info(_LI("Policy '%(id)s' is updated."), {'id': policy_id})
        return policy.to_dict()

    @request_context
    def policy_add_rules(self, cnxt, policy_id, rules):

        LOG.info(_LI("Adding rules '%(rules)s' to policy '%(policy)s'."),
                 {'policy': policy_id, 'rules': rules})
        policy = policy_mod.Policy.load(cnxt, policy_id=policy_id)
        exist_types = [r['type'] for r in policy.rules]

        error_rules = []
        ok_rules = []
        not_found = []
        for rule in rules:
            try:
                db_rule = rule_base.Rule.load(cnxt, rule_id=rule)
                append_data = {'id': db_rule.id, 'type': db_rule.type}
                if db_rule.type in exist_types:
                    error_rules.append(append_data)
                else:
                    ok_rules.append(append_data)
            except exception.RuleNotFound:
                not_found.append(rule)
                pass

        error = None
        if len(error_rules) > 0:
            error = _("Rule types of rules %(rules)s exist in policy "
                      "%(policy)s.") % {'rules': error_rules,
                                        'policy': policy_id}
        if len(not_found) > 0:
            error = _("Rules not found: %s") % not_found

        if error is not None:
            LOG.error(error)
            raise exception.BileanBadRequest(msg=error)

        policy.rules += ok_rules
        policy.store(cnxt)
        return policy.to_dict()

    @request_context
    def policy_remove_rule(self, cnxt, policy_id, rule_ids):
        return NotImplemented

    @request_context
    def policy_delete(self, cnxt, policy_id):
        LOG.info(_LI("Deleting policy: '%s'."), policy_id)
        policy_mod.Policy.delete(cnxt, policy_id)
