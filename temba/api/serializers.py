from __future__ import unicode_literals

import json
import phonenumbers

from django.core.exceptions import ValidationError
from django.conf import settings
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from temba.campaigns.models import Campaign, CampaignEvent, FLOW_EVENT, MESSAGE_EVENT
from temba.channels.models import Channel
from temba.contacts.models import Contact, ContactField, ContactGroup, ContactURN, TEL_SCHEME
from temba.flows.models import Flow, FlowRun
from temba.locations.models import AdminBoundary
from temba.msgs.models import Msg, Call, Broadcast, Label, ARCHIVED, INCOMING
from temba.values.models import VALUE_TYPE_CHOICES


# ------------------------------------------------------------------------------------------
# Field types
# ------------------------------------------------------------------------------------------

class DictionaryField(serializers.WritableField):

    def to_native(self, obj):
        raise ValidationError("Reading of extra field not supported")

    def from_native(self, data):
        if isinstance(data, dict):
            for key in data.keys():
                value = data[key]

                if not isinstance(key, basestring) or not isinstance(value, basestring):
                    raise ValidationError("Invalid, keys and values must both be strings: %s" % unicode(value))
            return data
        else:
            raise ValidationError("Invalid, must be dictionary: %s" % data)


class IntegerArrayField(serializers.WritableField):

    def to_native(self, obj):
        raise ValidationError("Reading of integer array field not supported")

    def from_native(self, data):
        # single number case, this is ok
        if isinstance(data, int) or isinstance(data, long):
            return [data]
        # it's a list, make sure they are all numbers
        elif isinstance(data, list):
            for value in data:
                if not (isinstance(value, int) or isinstance(value, long)):
                    raise ValidationError("Invalid, values must be integers or longs: %s" % unicode(value))
            return data
        # none of the above, error
        else:
            raise ValidationError("Invalid, must be array: %s" % data)


class StringArrayField(serializers.WritableField):

    def to_native(self, obj):
        raise ValidationError("Reading of string array field not supported")

    def from_native(self, data):
        # single string case, this is ok
        if isinstance(data, basestring):
            return [data]
        # it's a list, make sure they are all strings
        elif isinstance(data, list):
            for value in data:
                if not isinstance(value, basestring):
                    raise ValidationError("Invalid, values must be strings: %s" % unicode(value))
            return data
        # none of the above, error
        else:
            raise ValidationError("Invalid, must be array: %s" % data)


class PhoneArrayField(serializers.WritableField):

    def to_native(self, obj):
        raise ValidationError("Reading of phone field not supported")

    def from_native(self, data):
        if isinstance(data, basestring):
            return [(TEL_SCHEME, data)]
        elif isinstance(data, list):
            if len(data) > 100:
                raise ValidationError("You can only specify up to 100 numbers at a time.")

            urns = []
            for phone in data:
                if not isinstance(phone, basestring):
                    raise ValidationError("Invalid phone: %s" % str(phone))
                urns.append((TEL_SCHEME, phone))

            return urns
        else:
            raise ValidationError("Invalid phone: %s" % data)


class FlowField(serializers.PrimaryKeyRelatedField):

    def __init__(self, **kwargs):
        super(FlowField, self).__init__(queryset=Flow.objects.filter(is_active=True), **kwargs)


class ChannelField(serializers.PrimaryKeyRelatedField):

    def __init__(self, **kwargs):
        super(ChannelField, self).__init__(queryset=Channel.objects.filter(is_active=True), **kwargs)


class UUIDField(serializers.CharField):

    def __init__(self, **kwargs):
        super(UUIDField, self).__init__(max_length=36, **kwargs)


# ------------------------------------------------------------------------------------------
# Serializers
# ------------------------------------------------------------------------------------------

class WriteSerializer(serializers.Serializer):

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user')
        self.org = kwargs.pop('org') if 'org' in kwargs else self.user.get_org()

        super(WriteSerializer, self).__init__(*args, **kwargs)

    def restore_fields(self, data, files):

        if not isinstance(data, dict):
            self._errors['non_field_errors'] = ["Request body should be a single JSON object"]
            return {}

        return super(WriteSerializer, self).restore_fields(data, files)


class MsgReadSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField('get_id')
    broadcast = serializers.SerializerMethodField('get_broadcast')
    contact = serializers.SerializerMethodField('get_contact_uuid')
    urn = serializers.SerializerMethodField('get_urn')
    status = serializers.SerializerMethodField('get_status')
    archived = serializers.SerializerMethodField('get_archived')
    relayer = serializers.SerializerMethodField('get_relayer')
    type = serializers.SerializerMethodField('get_type')
    labels = serializers.SerializerMethodField('get_labels')

    def get_id(self, obj):
        return obj.pk

    def get_broadcast(self, obj):
        return obj.broadcast_id

    def get_type(self, obj):
        return obj.msg_type

    def get_urn(self, obj):
        if obj.org.is_anon:
            return None
        return obj.contact_urn.urn

    def get_contact_uuid(self, obj):
        return obj.contact.uuid

    def get_relayer(self, obj):
        return obj.channel_id

    def get_status(self, obj):
        # PENDING and QUEUED are same as far as users are concerned
        return 'Q' if obj.status in ['Q', 'P'] else obj.status

    def get_archived(self, obj):
        return obj.visibility == ARCHIVED

    def get_labels(self, obj):
        return [l.name for l in obj.labels.all()]

    class Meta:
        model = Msg
        fields = ('id', 'broadcast', 'contact', 'urn', 'status', 'type', 'labels', 'relayer',
                  'direction', 'archived', 'text', 'created_on', 'sent_on', 'delivered_on')


class MsgBulkActionSerializer(WriteSerializer):
    messages = IntegerArrayField(required=True)
    action = serializers.CharField(required=True)
    label = serializers.CharField(required=False)
    label_uuid = serializers.CharField(required=False)

    def validate(self, attrs):
        if attrs['action'] in ('label', 'unlabel') and not ('label' in attrs or 'label_uuid' in attrs):
            raise ValidationError("For action %s you must also specify label or label_uuid" % attrs['action'])
        return attrs

    def validate_action(self, attrs, source):
        if attrs[source] not in ('label', 'unlabel', 'archive', 'unarchive', 'delete'):
            raise ValidationError("Invalid action name: %s" % attrs[source])
        return attrs

    def validate_label(self, attrs, source):
        label_name = attrs.get(source, None)
        if label_name:
            if not Label.is_valid_name(label_name):
                raise ValidationError("Label name must not be blank or begin with + or -")

            attrs['label'] = Label.get_or_create(self.org, self.user, label_name, None)
        return attrs

    def validate_label_uuid(self, attrs, source):
        label_uuid = attrs.get(source, None)
        if label_uuid:
            label = Label.user_labels.filter(org=self.org, uuid=label_uuid).first()
            if not label:
                raise ValidationError("No such label with UUID: %s" % label_uuid)
            attrs['label'] = label
        return attrs

    def restore_object(self, attrs, instance=None):
        if instance:  # pragma: no cover
            raise ValidationError("Invalid operation")

        msg_ids = attrs['messages']
        action = attrs['action']

        msgs = Msg.objects.filter(org=self.org, direction=INCOMING, pk__in=msg_ids)

        if action == 'label':
            attrs['label'].toggle_label(msgs, add=True)
        elif action == 'unlabel':
            attrs['label'].toggle_label(msgs, add=False)
        else:
            # these are in-efficient but necessary to keep cached counts correct. In future if counts are completely
            # driven by triggers these could be replaced with queryset bulk update operations.
            for msg in msgs:
                if action == 'archive':
                    msg.archive()
                elif action == 'unarchive':
                    msg.restore()
                elif action == 'delete':
                    msg.release()

    class Meta:
        fields = ('messages', 'action', 'label', 'label_uuid')


class LabelReadSerializer(serializers.ModelSerializer):
    uuid = serializers.Field(source='uuid')
    name = serializers.Field(source='name')
    count = serializers.SerializerMethodField('get_count')

    def get_count(self, obj):
        return obj.get_visible_count()

    class Meta:
        model = Label
        fields = ('uuid', 'name', 'count')


class LabelWriteSerializer(WriteSerializer):
    uuid = serializers.CharField(required=False)
    name = serializers.CharField(required=True)

    def validate_uuid(self, attrs, source):
        uuid = attrs.get(source, None)

        if uuid and not Label.user_labels.filter(org=self.org, uuid=uuid).exists():
            raise ValidationError("No such message label with UUID: %s" % uuid)

        return attrs

    def validate_name(self, attrs, source):
        uuid = attrs.get('uuid', None)
        name = attrs.get(source, None)

        if Label.user_labels.filter(org=self.org, name=name).exclude(uuid=uuid).exists():
            raise ValidationError("Label name must be unique")

        return attrs

    def restore_object(self, attrs, instance=None):
        if instance:  # pragma: no cover
            raise ValidationError("Invalid operation")

        uuid = attrs.get('uuid', None)
        name = attrs.get('name', None)

        if uuid:
            existing = Label.user_labels.get(org=self.org, uuid=uuid)
            existing.name = name
            existing.save()
            return existing
        else:
            return Label.get_or_create(self.org, self.user, name)


class ContactGroupReadSerializer(serializers.ModelSerializer):
    group = serializers.Field(source='id')  # deprecated, use uuid
    uuid = serializers.Field(source='uuid')
    name = serializers.Field(source='name')
    size = serializers.SerializerMethodField('get_size')

    def get_size(self, obj):
        return obj.get_member_count()

    class Meta:
        model = ContactGroup
        fields = ('group', 'uuid', 'name', 'size')


class ContactReadSerializer(serializers.ModelSerializer):
    name = serializers.Field(source='name')
    uuid = serializers.Field(source='uuid')
    language = serializers.Field(source='language')
    group_uuids = serializers.SerializerMethodField('get_group_uuids')
    urns = serializers.SerializerMethodField('get_urns')
    fields = serializers.SerializerMethodField('get_contact_fields')
    phone = serializers.SerializerMethodField('get_tel')  # deprecated, use urns
    groups = serializers.SerializerMethodField('get_groups')  # deprecated, use group_uuids

    def get_groups(self, obj):
        groups = obj.prefetched_user_groups if hasattr(obj, 'prefetched_user_groups') else obj.user_groups.all()
        return [_.name for _ in groups]

    def get_group_uuids(self, obj):
        groups = obj.prefetched_user_groups if hasattr(obj, 'prefetched_user_groups') else obj.user_groups.all()
        return [_.uuid for _ in groups]

    def get_urns(self, obj):
        if obj.org.is_anon:
            return dict()

        return [urn.urn for urn in obj.get_urns()]

    def get_contact_fields(self, obj):
        fields = dict()
        for contact_field in self.context['contact_fields']:
            fields[contact_field.key] = obj.get_field_display(contact_field.key)
        return fields

    def get_tel(self, obj):
        return obj.get_urn_display(obj.org, scheme=TEL_SCHEME, full=True)

    class Meta:
        model = Contact
        fields = ('uuid', 'name', 'language', 'group_uuids', 'urns', 'fields', 'modified_on', 'phone', 'groups')


class ContactWriteSerializer(WriteSerializer):
    uuid = serializers.CharField(required=False, max_length=36)
    name = serializers.CharField(required=False, max_length=64)
    language = serializers.CharField(required=False, max_length=4)
    urns = StringArrayField(required=False)
    group_uuids = StringArrayField(required=False)
    fields = DictionaryField(required=False)
    phone = serializers.CharField(required=False, max_length=16)  # deprecated, use urns
    groups = StringArrayField(required=False)  # deprecated, use group_uuids

    def validate(self, attrs):
        urns = attrs.get('urns', [])
        phone = attrs.get('phone', None)
        uuid = attrs.get('uuid', None)

        if (not urns and not phone and not uuid) or (urns and phone):
            raise ValidationError("Must provide either urns, phone or uuid but only one of each")

        if attrs.get('group_uuids', []) and attrs.get('groups', []):
            raise ValidationError("Parameter groups is deprecated and can't be used together with group_uuids")

        if uuid:
            if phone:
                urns = [(TEL_SCHEME, attrs['phone'])]

            if urns:
                urns_strings = ["%s:%s" % u for u in urns]
                urn_query = Q(pk__lt=0)
                for urn_string in urns_strings:
                    urn_query |= Q(urns__urn__iexact=urn_string)

                other_contacts = Contact.objects.filter(org=self.org)
                other_contacts = other_contacts.filter(urn_query).distinct()
                other_contacts = other_contacts.exclude(uuid=uuid)
                if other_contacts:
                    if phone:
                        raise ValidationError(_("phone %s is used by another contact") % phone)
                    raise ValidationError(_("URNs %s are used by other contacts") % urns_strings)

        return attrs

    def validate_language(self, attrs, source):
        if 'language' in attrs:
            language = attrs.get(source, None)
            supported_languages = [l.iso_code for l in self.user.get_org().languages.all()]

            if language:
                # no languages configured
                if not supported_languages:
                    raise ValidationError(_("You do not have any languages configured for your organization."))

                # is it one of the languages on this org?
                if not language.lower() in supported_languages:
                    raise ValidationError(_("Language code '%s' is not one of supported for organization. (%s)") %
                                          (language, ",".join(supported_languages)))

                attrs['language'] = language.lower()
            else:
                attrs['language'] = None

        return attrs

    def validate_uuid(self, attrs, source):
        uuid = attrs.get(source, '')
        if uuid:
            contact = Contact.objects.filter(org=self.org, uuid=uuid, is_active=True).first()
            if not contact:
                raise ValidationError("Unable to find contact with UUID: %s" % uuid)

        return attrs

    def validate_phone(self, attrs, source):
        phone = attrs.get(source, None)
        if phone:
            try:
                normalized = phonenumbers.parse(phone, None)
                if not phonenumbers.is_possible_number(normalized):
                    raise ValidationError("Invalid phone number: '%s'" % phone)
            except:  # pragma: no cover
                raise ValidationError("Invalid phone number: '%s'" % phone)

            phone = phonenumbers.format_number(normalized, phonenumbers.PhoneNumberFormat.E164)
            attrs['phone'] = phone
        return attrs

    def validate_urns(self, attrs, source):
        urns = None
        request_urns = attrs.get(source, None)

        if request_urns is not None:
            urns = []
            for urn in request_urns:
                try:
                    parsed = ContactURN.parse_urn(urn)
                except ValueError:
                    raise ValidationError("Unable to parse URN: '%s'" % urn)

                norm_scheme, norm_path = ContactURN.normalize_urn(parsed.scheme, parsed.path)

                if not ContactURN.validate_urn(norm_scheme, norm_path):
                    raise ValidationError("Invalid URN: '%s'" % urn)

                urns.append((norm_scheme, norm_path))

        attrs['urns'] = urns
        return attrs

    def validate_fields(self, attrs, source):
        fields = attrs.get(source, {}).items()
        if fields:
            org_fields = self.context['contact_fields']

            for key, value in attrs.get(source, {}).items():
                for field in org_fields:
                    # TODO get users to stop writing fields via labels
                    if field.key == key or field.label == key:
                        break
                else:
                    raise ValidationError("Invalid contact field key: '%s'" % key)

        return attrs

    def validate_groups(self, attrs, source):
        group_names = attrs.get(source, None)
        if group_names is not None:
            groups = []
            for name in group_names:
                if not ContactGroup.is_valid_name(name):
                    raise ValidationError(_("Invalid group name: '%s'") % name)
                groups.append(name)

            attrs['groups'] = groups
        return attrs

    def validate_group_uuids(self, attrs, source):
        group_uuids = attrs.get(source, None)
        if group_uuids is not None:
            groups = []
            for uuid in group_uuids:
                group = ContactGroup.user_groups.filter(uuid=uuid, org=self.org, is_active=True).first()
                if not group:
                    raise ValidationError(_("Unable to find contact group with uuid: %s") % uuid)

                groups.append(group)

            attrs['group_uuids'] = groups
        return attrs

    def restore_object(self, attrs, instance=None):
        """
        Update our contact
        """
        if instance:  # pragma: no cover
            raise ValidationError("Invalid operation")

        if self.org.is_anon:
            raise ValidationError("Cannot update contacts on anonymous organizations")

        uuid = attrs.get('uuid', None)
        if uuid:
            contact = Contact.objects.get(uuid=uuid, org=self.org, is_active=True)

        urns = attrs.get('urns', None)
        phone = attrs.get('phone', None)

        # user didn't specify either urns or phone, stick to what already exists
        if urns is None and phone is None:
            urns = [(u.scheme, u.path) for u in contact.urns.all()]

        # user only specified phone, build our urns from it
        if phone:
            urns = [(TEL_SCHEME, attrs['phone'])]

        if uuid:
            contact.update_urns(urns)
        else:
            contact = Contact.get_or_create(self.org, self.user, urns=urns, uuid=uuid)

        changed = []

        # update our name and language
        if attrs.get('name', None):
            contact.name = attrs['name']
            changed.append('name')

        if 'language' in attrs:
            contact.language = attrs['language']
            changed.append('language')

        # save our contact if it changed
        if changed:
            contact.save(update_fields=changed)

        # update our fields
        fields = attrs.get('fields', None)
        if not fields is None:
            for key, value in fields.items():
                existing_by_key = ContactField.objects.filter(org=self.org, key__iexact=key, is_active=True).first()
                if existing_by_key:
                    contact.set_field(existing_by_key.key, value)
                    continue

                # TODO as above, need to get users to stop updating via label
                existing_by_label = ContactField.get_by_label(self.org, key)
                if existing_by_label:
                    contact.set_field(existing_by_label.key, value)

        # update our groups by UUID or name (deprecated)
        group_uuids = attrs.get('group_uuids', None)
        group_names = attrs.get('groups', None)

        if not group_uuids is None:
            contact.update_groups(group_uuids)

        elif not group_names is None:
            # by name creates groups if necessary
            groups = [ContactGroup.get_or_create(self.org, self.user, name) for name in group_names]
            contact.update_groups(groups)

        return contact


class ContactFieldReadSerializer(serializers.ModelSerializer):
    key = serializers.Field(source='key')
    label = serializers.Field(source='label')
    value_type = serializers.Field(source='value_type')

    class Meta:
        model = ContactField
        fields = ('key', 'label', 'value_type')


class ContactFieldWriteSerializer(WriteSerializer):
    key = serializers.CharField(required=False)
    label = serializers.CharField(required=True)
    value_type = serializers.CharField(required=True)

    def validate_key(self, attrs, source):
        key = attrs.get(source, '')
        if key:
            # if key is specified, then we're updating a field, so key must exist
            if not ContactField.objects.filter(org=self.org, key=key).exists():
                raise ValidationError("No such contact field key")
        return attrs

    def validate_value_type(self, attrs, source):
        value_type = attrs.get(source, '')
        if value_type:
            if not value_type in [t for t, label in VALUE_TYPE_CHOICES]:
                raise ValidationError("Invalid field value type")
        return attrs

    def validate(self, attrs):

        key = attrs.get('key', None)
        label = attrs.get('label')

        if not key:
            key = ContactField.api_make_key(label)

        attrs['key'] = key
        return attrs

    def restore_object(self, attrs, instance=None):
        """
        Update our contact field
        """
        if instance:  # pragma: no cover
            raise ValidationError("Invalid operation")

        key = attrs.get('key')
        label = attrs.get('label')
        value_type = attrs.get('value_type')

        return ContactField.get_or_create(self.org, key, label, value_type=value_type)


class CampaignEventSerializer(serializers.ModelSerializer):
    campaign_uuid = serializers.SerializerMethodField('get_campaign_uuid')
    flow_uuid = serializers.SerializerMethodField('get_flow_uuid')
    relative_to = serializers.SerializerMethodField('get_relative_to')
    event = serializers.SerializerMethodField('get_event')  # deprecated, use uuid
    campaign = serializers.SerializerMethodField('get_campaign')  # deprecated, use campaign_uuid
    flow = serializers.SerializerMethodField('get_flow')  # deprecated, use flow_uuid

    def get_campaign_uuid(self, obj):
        return obj.campaign.uuid

    def get_flow_uuid(self, obj):
        return obj.flow.uuid if obj.event_type == FLOW_EVENT else None

    def get_campaign(self, obj):
        return obj.campaign.pk

    def get_event(self, obj):
        return obj.pk

    def get_flow(self, obj):
        return obj.flow_id if obj.event_type == FLOW_EVENT else None

    def get_relative_to(self, obj):
        return obj.relative_to.label

    class Meta:
        model = CampaignEvent
        fields = ('uuid', 'campaign_uuid', 'flow_uuid', 'relative_to', 'offset', 'unit', 'delivery_hour', 'message',
                  'created_on', 'event', 'campaign', 'flow')


class CampaignEventWriteSerializer(WriteSerializer):
    uuid = UUIDField(required=False)
    campaign_uuid = UUIDField(required=False)
    offset = serializers.IntegerField(required=True)
    unit = serializers.CharField(required=True, max_length=1)
    delivery_hour = serializers.IntegerField(required=True)
    relative_to = serializers.CharField(required=True, min_length=3, max_length=64)
    message = serializers.CharField(required=False, max_length=320)
    flow_uuid = UUIDField(required=False)
    event = serializers.IntegerField(required=False)  # deprecated, use uuid
    campaign = serializers.IntegerField(required=False)  # deprecated, use campaign_uuid
    flow = serializers.IntegerField(required=False)  # deprecated, use flow_uuid

    def validate_event(self, attrs, source):
        event_id = attrs.get(source, None)
        if event_id:
            event = CampaignEvent.objects.filter(pk=event_id, is_active=True, campaign__org=self.org).first()
            if event:
                attrs['event_obj'] = event
            else:
                raise ValidationError("No event with id %d" % event_id)

        return attrs

    def validate_uuid(self, attrs, source):
        uuid = attrs.get(source, None)
        if uuid:
            event = CampaignEvent.objects.filter(uuid=uuid, is_active=True, campaign__org=self.org).first()
            if event:
                attrs['event_obj'] = event
            else:
                raise ValidationError("No event with UUID %s" % uuid)

        return attrs

    def validate_campaign(self, attrs, source):
        campaign_id = attrs.get(source, None)
        if campaign_id:
            campaign = Campaign.get_campaigns(self.org).filter(pk=campaign_id).first()
            if campaign:
                attrs['campaign_obj'] = campaign
            else:
                raise ValidationError("No campaign with id %d" % campaign_id)

        return attrs

    def validate_campaign_uuid(self, attrs, source):
        campaign_uuid = attrs.get(source, None)
        if campaign_uuid:
            campaign = Campaign.get_campaigns(self.org).filter(uuid=campaign_uuid).first()
            if campaign:
                attrs['campaign_obj'] = campaign
            else:
                raise ValidationError("No campaign with UUID %s" % campaign_uuid)

        return attrs

    def validate_unit(self, attrs, source):
        unit = attrs[source]

        if unit not in ["M", "H", "D", "W"]:
            raise ValidationError("Unit must be one of M, H, D or W for Minute, Hour, Day or Week")

        return attrs

    def validate_delivery_hour(self, attrs, source):
        delivery_hour = attrs[source]

        if delivery_hour < -1 or delivery_hour > 23:
            raise ValidationError("Delivery hour must be either -1 (for same hour) or 0-23")

        return attrs

    def validate_flow(self, attrs, source):
        flow_id = attrs.get(source, None)
        if flow_id:
            flow = Flow.objects.filter(pk=flow_id, is_active=True, org=self.org).first()
            if flow:
                attrs['flow_obj'] = flow
            else:
                raise ValidationError("No flow with id %d" % flow_id)

        return attrs

    def validate_flow_uuid(self, attrs, source):
        flow_uuid = attrs.get(source, None)
        if flow_uuid:
            flow = Flow.objects.filter(uuid=flow_uuid, is_active=True, org=self.org).first()
            if flow:
                attrs['flow_obj'] = flow
            else:
                raise ValidationError("No flow with UUID %s" % flow_uuid)

        return attrs

    def validate(self, attrs):
        if not (attrs.get('message', None) or attrs.get('flow_obj', None)):
            raise ValidationError("Must specify either a flow or a message for the event")

        if attrs.get('message', None) and attrs.get('flow_obj', None):
            raise ValidationError("Events cannot have both a message and a flow")

        if attrs.get('event_obj', None) and attrs.get('campaign_obj', None):
            raise ValidationError("Cannot specify campaign if updating an existing event")

        return attrs

    def restore_object(self, attrs, instance=None):
        """
        Create or update our campaign
        """
        if instance:  # pragma: no cover
            raise ValidationError("Invalid operation")

        # parse our arguments
        event = attrs.get('event_obj', None)
        campaign = attrs.get('campaign_obj')
        offset = attrs.get('offset')
        unit = attrs.get('unit')
        delivery_hour = attrs.get('delivery_hour')
        relative_to_label = attrs.get('relative_to')
        message = attrs.get('message', None)
        flow = attrs.get('flow_obj', None)

        # ensure contact field exists
        relative_to = ContactField.get_by_label(self.org, relative_to_label)
        if not relative_to:
            key = ContactField.api_make_key(relative_to_label)
            relative_to = ContactField.get_or_create(self.org, key, relative_to_label)

        if event:
            # we are being set to a flow
            if flow:
                event.flow = flow
                event.event_type = FLOW_EVENT
                event.message = None

            # we are being set to a message
            else:
                event.message = message

                # if we aren't currently a message event, we need to create our hidden message flow
                if event.event_type != MESSAGE_EVENT:
                    event.flow = Flow.create_single_message(self.org, self.user, event.message)
                    event.event_type = MESSAGE_EVENT

                # otherwise, we can just update that flow
                else:
                    # set our single message on our flow
                    event.flow.update_single_message_flow(message=attrs['message'])

            # update our other attributes
            event.offset = offset
            event.unit = unit
            event.delivery_hour = delivery_hour
            event.relative_to = relative_to
            event.save()
            event.update_flow_name()

        else:
            if flow:
                event = CampaignEvent.create_flow_event(self.org, self.user, campaign,
                                                        relative_to, offset, unit, flow, delivery_hour)
            else:
                event = CampaignEvent.create_message_event(self.org, self.user, campaign,
                                                           relative_to, offset, unit, message, delivery_hour)
            event.update_flow_name()

        return event


class CampaignSerializer(serializers.ModelSerializer):
    group_uuid = serializers.SerializerMethodField('get_group_uuid')
    group = serializers.SerializerMethodField('get_group')  # deprecated, use group_uuid
    campaign = serializers.SerializerMethodField('get_campaign')  # deprecated, use uuid

    def get_group_uuid(self, obj):
        return obj.group.uuid

    def get_campaign(self, obj):
        return obj.pk

    def get_group(self, obj):
        return obj.group.name

    class Meta:
        model = Campaign
        fields = ('uuid', 'name', 'group_uuid', 'created_on', 'campaign', 'group')


class CampaignWriteSerializer(WriteSerializer):
    uuid = UUIDField(required=False)
    name = serializers.CharField(required=True, max_length=64)
    group_uuid = UUIDField(required=False)
    campaign = serializers.IntegerField(required=False)  # deprecated, use uuid
    group = serializers.CharField(required=False, max_length=64)  # deprecated, use group_uuid

    def validate_uuid(self, attrs, source):
        uuid = attrs.get(source, None)
        if uuid:
            campaign = Campaign.get_campaigns(self.org).filter(uuid=uuid).first()
            if campaign:
                attrs['campaign_obj'] = campaign
            else:
                raise ValidationError("No campaign with UUID %s" % uuid)

        return attrs

    def validate_campaign(self, attrs, source):
        campaign_id = attrs.get(source, None)
        if campaign_id:
            campaign = Campaign.get_campaigns(self.org).filter(pk=campaign_id).first()
            if campaign:
                attrs['campaign_obj'] = campaign
            else:
                raise ValidationError("No campaign with id %d" % campaign_id)

        return attrs

    def validate_group_uuid(self, attrs, source):
        group_uuid = attrs.get(source, None)
        if group_uuid:
            group = ContactGroup.user_groups.filter(org=self.org, is_active=True, uuid=group_uuid).first()
            if group:
                attrs['group_obj'] = group
            else:
                raise ValidationError("No contact group with UUID %s" % group_uuid)

        return attrs

    def validate(self, attrs):
        if not attrs.get('group', None) and not attrs.get('group_uuid', None):
            raise ValidationError("Must specify either group name or group_uuid")

        if attrs.get('campaign', None) and attrs.get('uuid', None):
            raise ValidationError("Can't specify both campaign and uuid")

        return attrs

    def restore_object(self, attrs, instance=None):
        """
        Create or update our campaign
        """
        if instance:  # pragma: no cover
            raise ValidationError("Invalid operation")

        if 'group_obj' in attrs:
            group = attrs['group_obj']
        else:
            group = ContactGroup.get_or_create(self.org, self.user, attrs['group'])

        campaign = attrs.get('campaign_obj', None)

        if campaign:
            campaign.name = attrs['name']
            campaign.group = group
            campaign.save()
        else:
            campaign = Campaign.create(self.org, self.user, attrs['name'], group)

        return campaign


class FlowReadSerializer(serializers.ModelSerializer):
    uuid = serializers.Field(source='uuid')
    archived = serializers.Field(source='is_archived')
    expires = serializers.Field(source='expires_after_minutes')
    labels = serializers.SerializerMethodField('get_labels')
    rulesets = serializers.SerializerMethodField('get_rulesets')
    runs = serializers.SerializerMethodField('get_runs')
    completed_runs = serializers.SerializerMethodField('get_completed_runs')
    participants = serializers.SerializerMethodField('get_participants')
    flow = serializers.Field(source='id')  # deprecated, use uuid

    def get_runs(self, obj):
        return obj.get_total_runs()

    def get_labels(self, obj):
        return [l.name for l in obj.labels.all()]

    def get_completed_runs(self, obj):
        return obj.get_completed_runs()

    def get_participants(self, obj):
        return obj.get_total_contacts()

    def get_rulesets(self, obj):
        rulesets = list()
        for ruleset in obj.rule_sets.all().order_by('y'):
            rulesets.append(dict(node=ruleset.uuid,
                                 label=ruleset.label,
                                 response_type=ruleset.response_type,
                                 id=ruleset.id))  # deprecated

        return rulesets

    class Meta:
        model = Flow
        fields = ('uuid', 'archived', 'expires', 'name', 'labels', 'participants', 'runs', 'completed_runs', 'rulesets',
                  'created_on', 'flow')


class FlowWriteSerializer(WriteSerializer):
    uuid = serializers.CharField(required=False, max_length=36)
    name = serializers.CharField(required=True)
    flow_type = serializers.CharField(required=True)
    definition = serializers.WritableField(required=False)

    def validate_uuid(self, attrs, source):
        uuid = attrs.get(source, None)

        if uuid and not Flow.objects.filter(org=self.org, uuid=uuid).exists():
            raise ValidationError("No such flow with UUID: %s" % uuid)
        return attrs

    def validate_flow_type(self, attrs, source):
        flow_type = attrs.get(source, None)

        if flow_type not in [choice[0] for choice in Flow.FLOW_TYPES]:
            raise ValidationError("Invalid flow type: %s" % flow_type)
        return attrs

    def restore_object(self, attrs, instance=None):
        """
        Update our flow
        """
        if instance:  # pragma: no cover
            raise ValidationError("Invalid operation")

        uuid = attrs.get('uuid', None)
        name = attrs['name']
        flow_type = attrs['flow_type']
        definition = attrs.get('definition', None)

        if uuid:
            flow = Flow.objects.get(org=self.org, uuid=uuid)
            flow.name = name
            flow.flow_type = flow_type
            flow.save()
        else:
            flow = Flow.create(self.org, self.user, name, flow_type)

        if definition:
            flow.update(definition, self.user, force=True)

        return flow


class FlowRunStartSerializer(WriteSerializer):
    flow_uuid = serializers.CharField(required=False, max_length=36)
    groups = StringArrayField(required=False)
    contacts = StringArrayField(required=False)
    extra = DictionaryField(required=False)
    restart_participants = serializers.BooleanField(required=False, default=True)
    flow = FlowField(required=False)  # deprecated, use flow_uuid
    contact = StringArrayField(required=False)  # deprecated, use contacts
    phone = PhoneArrayField(required=False)  # deprecated

    def validate(self, attrs):
        if not (attrs.get('flow', None) or attrs.get('flow_uuid', None)):
            raise ValidationError("Use flow_uuid to specify which flow to start")
        return attrs

    def validate_flow_uuid(self, attrs, source):
        flow_uuid = attrs.get(source, None)
        if flow_uuid:
            flow = Flow.objects.get(uuid=flow_uuid)
            if flow.is_archived:
                raise ValidationError("You cannot start an archived flow.")

            # do they have permission to use this flow?
            if self.org != flow.org:
                raise ValidationError("Invalid UUID '%s' - flow does not exist." % flow.uuid)

            attrs['flow'] = flow
        return attrs

    def validate_flow(self, attrs, source):
        flow = attrs.get(source, None)
        if flow:
            if flow.is_archived:
                raise ValidationError("You cannot start an archived flow.")

            # do they have permission to use this flow?
            if self.org != flow.org:
                raise ValidationError("Invalid pk '%d' - flow does not exist." % flow.id)

        return attrs

    def validate_groups(self, attrs, source):
        groups = []
        for uuid in attrs.get(source, []):
            group = ContactGroup.user_groups.filter(uuid=uuid, org=self.org, is_active=True).first()
            if not group:
                raise ValidationError(_("Unable to find contact group with uuid: %s") % uuid)

            groups.append(group)

        attrs['groups'] = groups
        return attrs

    def validate_contacts(self, attrs, source):
        contacts = []
        uuids = attrs.get(source, [])
        if uuids:
            for uuid in uuids:
                contact = Contact.objects.filter(uuid=uuid, org=self.org, is_active=True).first()
                if not contact:
                    raise ValidationError(_("Unable to find contact with uuid: %s") % uuid)

                contacts.append(contact)

            attrs['contacts'] = contacts
        return attrs

    def validate_contact(self, attrs, source):  # deprecated, use contacts
        contacts = []
        uuids = attrs.get(source, [])
        if uuids:
            for uuid in attrs.get(source, []):
                contact = Contact.objects.filter(uuid=uuid, org=self.org, is_active=True).first()
                if not contact:
                    raise ValidationError(_("Unable to find contact with uuid: %s") % uuid)

                contacts.append(contact)

            attrs['contacts'] = contacts
        return attrs

    def validate_phone(self, attrs, source):  # deprecated, use contacts
        if self.org.is_anon:
            raise ValidationError("Cannot start flows for anonymous organizations")

        numbers = attrs.get(source, [])
        if numbers:
            # get a channel
            channel = self.org.get_send_channel(TEL_SCHEME)

            if channel:
                # check our numbers for validity
                for tel, phone in numbers:
                    try:
                        normalized = phonenumbers.parse(phone, channel.country.code)
                        if not phonenumbers.is_possible_number(normalized):
                            raise ValidationError("Invalid phone number: '%s'" % phone)
                    except:
                        raise ValidationError("Invalid phone number: '%s'" % phone)
            else:
                raise ValidationError("You cannot start a flow for a phone number without a phone channel")

        return attrs

    def save(self):
        pass

    def restore_object(self, attrs, instance=None):
        """
        Actually start our flows for each contact
        """
        if instance:  # pragma: no cover
            raise ValidationError("Invalid operation")

        flow = attrs['flow']
        groups = attrs.get('groups', [])
        contacts = attrs.get('contacts', [])
        extra = attrs.get('extra', None)
        restart_participants = attrs.get('restart_participants', True)

        # include contacts created/matched via deprecated phone field
        phone_urns = attrs.get('phone', [])
        if phone_urns:
            channel = self.org.get_send_channel(TEL_SCHEME)
            for urn in phone_urns:
                # treat each URN as separate contact
                contact = Contact.get_or_create(channel.org, self.user, urns=[urn])
                contacts.append(contact)

        if contacts or groups:
            return flow.start(groups, contacts, restart_participants=restart_participants, extra=extra)
        else:
            return []


class BoundarySerializer(serializers.ModelSerializer):
    boundary = serializers.SerializerMethodField('get_boundary')
    parent = serializers.SerializerMethodField('get_parent')
    geometry = serializers.SerializerMethodField('get_geometry')

    def get_parent(self, obj):
        return obj.parent.osm_id if obj.parent else None

    def get_geometry(self, obj):
        return json.loads(obj.simplified_geometry.geojson)

    def get_boundary(self, obj):
        return obj.osm_id

    class Meta:
        model = AdminBoundary
        fields = ('boundary', 'name', 'level', 'parent', 'geometry')


class FlowRunReadSerializer(serializers.ModelSerializer):
    run = serializers.Field(source='id')
    flow_uuid = serializers.SerializerMethodField('get_flow_uuid')
    values = serializers.SerializerMethodField('get_values')
    steps = serializers.SerializerMethodField('get_steps')
    contact = serializers.SerializerMethodField('get_contact_uuid')
    completed = serializers.SerializerMethodField('is_completed')
    expires_on = serializers.Field(source='expires_on')
    expired_on = serializers.Field(source='expired_on')
    flow = serializers.SerializerMethodField('get_flow')  # deprecated, use flow_uuid

    def get_flow(self, obj):
        return obj.flow_id

    def get_flow_uuid(self, obj):
        return obj.flow.uuid

    def get_contact_uuid(self, obj):
        return obj.contact.uuid

    def is_completed(self, obj):
        return obj.is_completed()

    def get_values(self, obj):
        results = obj.flow.get_results(obj.contact, run=obj)
        if results:
            return results[0]['values']
        else:
            return []

    def get_steps(self, obj):
        steps = []
        for step in obj.steps.all():
            steps.append(dict(type=step.step_type,
                              node=step.step_uuid,
                              arrived_on=step.arrived_on,
                              left_on=step.left_on,
                              text=step.get_text(),
                              value=unicode(step.rule_value)))

        return steps

    class Meta:
        model = FlowRun
        fields = ('flow_uuid', 'flow', 'run', 'contact', 'completed', 'values',
                  'steps', 'created_on', 'expires_on', 'expired_on')


class BroadcastReadSerializer(serializers.ModelSerializer):
    id = serializers.Field(source='id')
    urns = serializers.SerializerMethodField('get_urns')
    contacts = serializers.SerializerMethodField('get_contacts')
    groups = serializers.SerializerMethodField('get_groups')
    text = serializers.Field(source='text')
    created_on = serializers.Field(source='created_on')
    status = serializers.Field(source='status')

    def get_urns(self, obj):
        return [urn.urn for urn in obj.urns.all()]

    def get_contacts(self, obj):
        return [contact.uuid for contact in obj.contacts.all()]

    def get_groups(self, obj):
        return [group.uuid for group in obj.groups.all()]

    class Meta:
        model = Broadcast
        fields = ('id', 'urns', 'contacts', 'groups', 'text', 'created_on', 'status')


class BroadcastCreateSerializer(WriteSerializer):
    urns = StringArrayField(required=False)
    contacts = StringArrayField(required=False)
    groups = StringArrayField(required=False)
    text = serializers.CharField(required=True, max_length=480)
    channel = ChannelField(required=False)

    def validate(self, attrs):
        if not (attrs.get('urns', []) or attrs.get('contacts', None) or attrs.get('groups', [])):
            raise ValidationError("Must provide either urns, contacts or groups")
        return attrs

    def validate_urns(self, attrs, source):
        # if we have tel URNs, we may need a country to normalize by
        tel_sender = self.org.get_send_channel(TEL_SCHEME)
        country = tel_sender.country if tel_sender else None

        urns = []
        for urn in attrs.get(source, []):
            try:
                parsed = ContactURN.parse_urn(urn)
            except ValueError, e:
                raise ValidationError(e.message)

            norm_scheme, norm_path = ContactURN.normalize_urn(parsed.scheme, parsed.path, country)
            if not ContactURN.validate_urn(norm_scheme, norm_path):
                raise ValidationError("Invalid URN: '%s'" % urn)
            urns.append((norm_scheme, norm_path))

        attrs[source] = urns
        return attrs

    def validate_contacts(self, attrs, source):
        contacts = []
        for uuid in attrs.get(source, []):
            contact = Contact.objects.filter(uuid=uuid, org=self.org, is_active=True).first()
            if not contact:
                raise ValidationError(_("Unable to find contact with uuid: %s") % uuid)
            contacts.append(contact)

        attrs[source] = contacts
        return attrs

    def validate_groups(self, attrs, source):
        groups = []
        for uuid in attrs.get(source, []):
            group = ContactGroup.user_groups.filter(uuid=uuid, org=self.org, is_active=True).first()
            if not group:
                raise ValidationError(_("Unable to find contact group with uuid: %s") % uuid)
            groups.append(group)

        attrs[source] = groups
        return attrs

    def validate_channel(self, attrs, source):
        channel = attrs.get(source, None)

        if channel:
            # do they have permission to use this channel?
            if not (channel.is_active and channel.org == self.org):
                raise ValidationError("Invalid pk '%d' - object does not exist." % channel.id)
        return attrs

    def restore_object(self, attrs, instance=None):
        """
        Create a new broadcast to send out
        """
        from temba.msgs.tasks import send_broadcast_task

        if instance:  # pragma: no cover
            raise ValidationError("Invalid operation")

        recipients = attrs.get('contacts') + attrs.get('groups')

        for urn in attrs.get('urns'):
            # create contacts for URNs if necessary
            contact = Contact.get_or_create(self.org, self.user, urns=[urn])
            contact_urn = contact.urn_objects[urn]
            recipients.append(contact_urn)

        # create the broadcast
        broadcast = Broadcast.create(self.org, self.user, attrs['text'],
                                     recipients=recipients, channel=attrs['channel'])

        # send in task
        send_broadcast_task.delay(broadcast.id)
        return broadcast


class MsgCreateSerializer(WriteSerializer):
    channel = ChannelField(required=False)
    text = serializers.CharField(required=True, max_length=480)
    urn = StringArrayField(required=False)
    contact = StringArrayField(required=False)
    phone = PhoneArrayField(required=False)

    def validate(self, attrs):
        urns = attrs.get('urn', [])
        phone = attrs.get('phone', None)
        contact = attrs.get('contact', [])
        if (not urns and not phone and not contact) or (urns and phone):
            raise ValidationError("Must provide either urns or phone or contact and not both")
        return attrs

    def validate_channel(self, attrs, source):
        channel = attrs[source]
        if not channel:
            channels = Channel.objects.filter(is_active=True, org=self.org).order_by('-last_seen')

            if not channels:
                raise ValidationError("There are no channels for this organization.")
            else:
                channel = channels[0]
                attrs[source] = channel

        # do they have permission to use this channel?
        if self.org != channel.org:
            raise ValidationError("Invalid pk '%d' - object does not exist." % channel.id)

        return attrs

    def validate_contact(self, attrs, source):
        contacts = []

        for uuid in attrs.get(source, []):
            contact = Contact.objects.filter(uuid=uuid, org=self.org, is_active=True).first()
            if not contact:
                raise ValidationError(_("Unable to find contact with uuid: %s") % uuid)

            contacts.append(contact)

        attrs['contact'] = contacts
        return attrs

    def validate_urn(self, attrs, source):
        urns = []

        if 'channel' in attrs and attrs['channel']:
            country = attrs['channel'].country

            for urn in attrs.get(source, []):
                parsed = ContactURN.parse_urn(urn)
                norm_scheme, norm_path = ContactURN.normalize_urn(parsed.scheme, parsed.path, country)
                if not ContactURN.validate_urn(norm_scheme, norm_path):
                    raise ValidationError("Invalid URN: '%s'" % urn)
                urns.append((norm_scheme, norm_path))
        else:
            raise ValidationError("You must specify a valid channel")

        attrs['urn'] = urns
        return attrs

    def validate_phone(self, attrs, source):
        if self.org.is_anon:
            raise ValidationError("Cannot create messages for anonymous organizations")

        if 'channel' in attrs and attrs['channel']:
            # check our numbers for validity
            country = attrs['channel'].country
            for tel, phone in attrs.get(source, []):
                try:
                    normalized = phonenumbers.parse(phone, country.code)
                    if not phonenumbers.is_possible_number(normalized):
                        raise ValidationError("Invalid phone number: '%s'" % phone)
                except:
                    raise ValidationError("Invalid phone number: '%s'" % phone)
        else:
            raise ValidationError("You must specify a valid channel")

        return attrs

    def restore_object(self, attrs, instance=None):
        """
        Create a new broadcast to send out
        """
        if instance: # pragma: no cover
            raise ValidationError("Invalid operation")

        if 'urn' in attrs and attrs['urn']:
            urns = attrs.get('urn', [])
        else:
            urns = attrs.get('phone', [])

        channel = attrs['channel']
        contacts = list()
        for urn in urns:
            # treat each urn as a separate contact
            contacts.append(Contact.get_or_create(channel.org, self.user, urns=[urn]))

        # add any contacts specified by uuids
        uuid_contacts = attrs.get('contact', [])
        for contact in uuid_contacts:
            contacts.append(contact)

        # create the broadcast
        broadcast = Broadcast.create(self.org, self.user, attrs['text'], recipients=contacts)

        # send it
        broadcast.send()
        return broadcast


class MsgCreateResultSerializer(serializers.ModelSerializer):
    messages = serializers.SerializerMethodField('get_messages')
    sms = serializers.SerializerMethodField('get_messages')  # deprecated

    def get_messages(self, obj):
        return [msg.id for msg in obj.get_messages()]

    class Meta:
        model = Broadcast
        fields = ('messages', 'sms')


class CallSerializer(serializers.ModelSerializer):
    call = serializers.SerializerMethodField('get_call')
    contact = serializers.SerializerMethodField('get_contact_uuid')
    created_on = serializers.Field(source='time')
    phone = serializers.SerializerMethodField('get_phone')
    relayer = serializers.SerializerMethodField('get_relayer')
    relayer_phone = serializers.SerializerMethodField('get_relayer_phone')

    def get_relayer_phone(self, obj):
        if obj.channel and obj.channel.address:
            return obj.channel.address
        else:
            return None

    def get_relayer(self, obj):
        if obj.channel:
            return obj.channel.pk
        else:
            return None

    def get_contact_uuid(self, obj):
        return obj.contact.uuid

    def get_phone(self, obj):
        return obj.contact.get_urn_display(org=obj.org, scheme=TEL_SCHEME, full=True)

    def get_call(self, obj):
        return obj.pk

    class Meta:
        model = Call
        fields = ('call', 'contact', 'relayer', 'relayer_phone', 'phone', 'created_on', 'duration', 'call_type')


class ChannelReadSerializer(serializers.ModelSerializer):
    relayer = serializers.SerializerMethodField('get_relayer')
    phone = serializers.SerializerMethodField('get_phone')
    power_level = serializers.Field(source='get_last_power')
    power_status = serializers.Field(source='get_last_power_status')
    power_source = serializers.Field(source='get_last_power_source')
    network_type = serializers.Field(source='get_last_network_type')
    pending_message_count = serializers.SerializerMethodField('get_unsent_count')

    def get_phone(self, obj):
        return obj.address

    def get_relayer(self, obj):
        return obj.pk

    def get_unsent_count(self, obj):
        return obj.get_unsent_messages().count()

    class Meta:
        model = Channel
        fields = ('relayer', 'phone', 'name', 'country', 'last_seen', 'power_level', 'power_status', 'power_source',
                  'network_type', 'pending_message_count')
        read_only_fields = ('last_seen',)


class ChannelClaimSerializer(WriteSerializer):
    claim_code = serializers.CharField(required=True, max_length=16)
    phone = serializers.CharField(required=True, max_length=16, source='number')
    name = serializers.CharField(required=False, max_length=64)

    def validate_claim_code(self, attrs, source):
        claim_code = attrs[source].strip()

        if not claim_code:
            raise ValidationError("Invalid claim code: '%s'" % claim_code)

        channel = Channel.objects.filter(claim_code=claim_code, is_active=True)
        if not channel:
            raise ValidationError("Invalid claim code: '%s'" % claim_code)

        attrs['channel'] = channel[0]
        return attrs

    def validate_phone(self, attrs, source):
        phone = attrs[source].strip()
        channel = attrs.get('channel', None)

        if not channel:
            return attrs

        try:
            normalized = phonenumbers.parse(phone, attrs['channel'].country.code)
            if not phonenumbers.is_possible_number(normalized):
                raise ValidationError("Invalid phone number: '%s'" % phone)
        except:  # pragma: no cover
            raise ValidationError("Invalid phone number: '%s'" % phone)

        phone = phonenumbers.format_number(normalized, phonenumbers.PhoneNumberFormat.E164)
        attrs['phone'] = phone

        return attrs

    def restore_object(self, attrs, instance=None):
        """
        Claim our channel
        """
        if instance: # pragma: no cover
            raise ValidationError("Invalid operation")

        channel = attrs['channel']
        if attrs.get('name', None):
            channel.name = attrs['name']

        channel.claim(self.org, attrs['phone'], self.user)

        if not settings.TESTING:  # pragma: no cover
            channel.trigger_sync()

        return attrs['channel']


