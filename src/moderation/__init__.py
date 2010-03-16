from django.db.models import base
from moderation.models import ModeratedObject, MODERATION_STATUS_PENDING,\
    MODERATION_STATUS_APPROVED
from django.core.exceptions import ObjectDoesNotExist
from django.db.models.manager import Manager
from django.contrib.contenttypes import generic
from moderation.managers import ModerationObjectsManager
from django.contrib.sites.models import Site
from django.template.loader import render_to_string
from django.core.mail import send_mail
from django.conf import settings
from moderation.notifications import BaseModerationNotification


class RegistrationError(Exception):
    """Exception thrown when registration with Moderation goes wrong."""


class GenericModerator(object):
    """
    Encapsulates moderation options for a given model.
    """
    manager_names = ['objects']
    moderation_manager_class = ModerationObjectsManager
    
    auto_approve_for_staff = True
    auto_approve_for_groups = None
    auto_reject_for_groups = None
    
    notify_moderator = True
    notify_user = True
    
    subject_template_moderator\
     = 'moderation/notification_subject_moderator.txt'
    message_template_moderator\
     = 'moderation/notification_message_moderator.txt'
    
    subject_template_user = 'moderation/notification_subject_user.txt'
    message_template_user = 'moderation/notification_message_user.txt'
    
    def __init__(self, model_class):
        self.model_class = model_class
        self.base_managers = self._get_base_managers()
        
    def send(self, content_object, subject_template, message_template,
                           recipient_list, extra_context=None):
        context = {
                'moderated_object': content_object.moderated_object,
                'content_object': content_object,
                'site': Site.objects.get_current(),
                'content_type': content_object.moderated_object.content_type}

        if extra_context:
            context.update(extra_context)

        message = render_to_string(message_template, context)
        subject = render_to_string(subject_template, context)

        send_mail(subject=subject,
                  message=message,
                  from_email=settings.DEFAULT_FROM_EMAIL,
                  recipient_list=recipient_list)

    def inform_moderator(self,
            content_object,
            extra_context=None):
        '''Send notification to moderator'''
        from moderation.conf.settings import MODERATORS
        if self.notify_moderator:
            self.send(content_object=content_object,
                  subject_template=self.subject_template_moderator,
                  message_template=self.message_template_moderator,
                  recipient_list=MODERATORS)

    def inform_user(self, content_object,
                user,
                extra_context=None):
        '''Send notification to user when object is approved or rejected'''
        if extra_context:
            extra_context.update({'user': user})
        else:
            extra_context = {'user': user}
        if self.notify_user:
            self.send(content_object=content_object,
                  subject_template=self.subject_template_user,
                  message_template=self.message_template_user,
                  recipient_list=[user.email],
                   extra_context=extra_context)
    
    def _get_base_managers(self):
        base_managers = []
        
        for manager_name in self.manager_names:
            base_managers.append(
                        (manager_name,
                        self._get_base_manager(self.model_class,
                                               manager_name)))
        return base_managers
    
    def _get_base_manager(self, model_class, manager_name):
        """Returns base manager class for given model class """
        if hasattr(model_class, manager_name):
            base_manager = getattr(model_class, manager_name).__class__
        else:
            base_manager = Manager

        return base_manager


class ModerationManager(object):

    def __init__(self):
        """Initializes the moderation manager."""
        self._registered_models = {}

    def register(self, model_class, moderator_class=None):
        """Registers model class with moderation"""
        if model_class in self._registered_models:
            msg = u"%s has been registered with Moderation." % model_class
            raise RegistrationError(msg)
        if not moderator_class:
            moderator_class = GenericModerator

        if not issubclass(moderator_class, GenericModerator):
            msg = 'moderator_class must subclass '\
                  'GenericModerator class, found %s' % moderator_class
            raise AttributeError(msg)

        self._registered_models[model_class] = moderator_class(model_class)

        self._and_fields_to_model_class(self._registered_models[model_class])
        self._connect_signals(model_class)

    def _connect_signals(self, model_class):
        from django.db.models import signals
        signals.pre_save.connect(self.pre_save_handler,
                                     sender=model_class)
        signals.post_save.connect(self.post_save_handler,
                                      sender=model_class)
    
    def _add_moderated_object_to_class(self, model_class):
        relation_object = generic.GenericRelation(ModeratedObject,
                                               object_id_field='object_pk')
        
        model_class.add_to_class('_moderated_object', relation_object)

        def get_modarated_object(self):
            return getattr(self, '_moderated_object').get()

        model_class.add_to_class('moderated_object',
                                 property(get_modarated_object))

    def _and_fields_to_model_class(self, moderator_class_instance):
        """Sets moderation manager on model class,
           adds generic relation to ModeratedObject,
           sets _default_manager on model class as instance of
           ModerationObjectsManager
        """
        model_class = moderator_class_instance.model_class
        base_managers = moderator_class_instance.base_managers 
        moderation_manager_class\
         = moderator_class_instance.moderation_manager_class

        for manager_name, mgr_class in base_managers:
            ModerationObjectsManager = moderation_manager_class()(mgr_class)
            manager = ModerationObjectsManager()
            model_class.add_to_class('unmoderated_%s' % manager_name,
                                     mgr_class())
            model_class.add_to_class(manager_name, manager)
            
        self._add_moderated_object_to_class(model_class)

    def unregister(self, model_class):
        """Unregister model class from moderation"""
        try:
            moderator_instance = self._registered_models.pop(model_class)
        except KeyError:
            msg = "%r has not been registered with Moderation." % model_class
            raise RegistrationError(msg)

        self._remove_fields(moderator_instance)
        self._disconnect_signals(model_class)

    def _remove_fields(self, moderator_class_instance):
        """Removes fields from model class and disconnects signals"""
        from django.db.models import signals
        model_class = moderator_class_instance.model_class
        base_managers = moderator_class_instance.base_managers
        
        for manager_name, manager_class in base_managers:
            manager = manager_class()
            delattr(model_class, 'unmoderated_%s' % manager_name)
            model_class.add_to_class(manager_name, manager)
            
        delattr(model_class, 'moderated_object')

    def _disconnect_signals(self, model_class):
        from django.db.models import signals
        signals.pre_save.disconnect(self.pre_save_handler, model_class)
        signals.post_save.disconnect(self.post_save_handler, model_class)

    def pre_save_handler(self, sender, instance, **kwargs):
        """Update moderation object when moderation object for
           existing instance of model does not exists
        """
        if instance.pk:
            moderated_object = self._get_or_create_moderated_object(instance)
            moderated_object.save()

    def _get_or_create_moderated_object(self, instance):
        """
        Get or create ModeratedObject instance.
        If moderated object is not equal instance then serialize unchanged
        in moderated object in order to use it later in post_save_handler
        """
        pk = instance.pk
        unchanged_obj = instance.__class__._default_manager.get(pk=pk)

        try:
            moderated_object = ModeratedObject.objects.get(object_pk=pk)

            if moderated_object._is_not_equal_instance(instance):
                moderated_object.changed_object = unchanged_obj

        except ObjectDoesNotExist:
            moderated_object = ModeratedObject(content_object=unchanged_obj)
            moderated_object.changed_object = unchanged_obj

        return moderated_object

    def post_save_handler(self, sender, instance, **kwargs):
        """
        Creates new moderation object if instance is created,
        If instance exists and is only updated then save instance as
        content_object of moderated_object
        """
        pk = instance.pk
        moderator_instance = self._registered_models[sender]
        if kwargs['created']:
            old_object = sender._default_manager.get(pk=pk)
            moderated_object = ModeratedObject(content_object=old_object)
            moderated_object.save()
            moderator_instance.inform_moderator(instance)
        else:

            moderated_object = ModeratedObject.objects.get(object_pk=pk)

            if moderated_object._is_not_equal_instance(instance):
                copied_instance = self._copy_model_instance(instance)
                # save instance with data from changed_object
                moderated_object.changed_object.save()

                # save new data in moderated object
                moderated_object.changed_object = copied_instance

                moderated_object.moderation_status = MODERATION_STATUS_PENDING
                moderated_object.save()
                moderator_instance.inform_moderator(instance)

    def _copy_model_instance(self, obj):
        initial = dict([(f.name, getattr(obj, f.name))
                    for f in obj._meta.fields
                    if not f in obj._meta.parents.values()])
        return obj.__class__(**initial)


moderation = ModerationManager()