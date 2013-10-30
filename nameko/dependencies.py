"""
Provides classes and method to deal with dependency injection.
"""
from abc import ABCMeta, abstractmethod
from functools import wraps, partial
import inspect
from itertools import chain
import types
from weakref import WeakSet, WeakKeyDictionary

from nameko.utils import SpawningSet


ENTRYPOINT_PROVIDERS_ATTR = 'nameko_entrypoints'


class NotInitializedError(Exception):
    pass


class DependencyTypeError(TypeError):
    pass


class DependencyProvider(object):

    def bind(self, name, container):
        """ Bind this DependencyProvider instance to ``container`` using the
        given ``name`` to identify the resource on the hosted service.

        Called during ServiceContainer initialisation. The DependencyProvider
        instance is created and then bound to the ServiceContainer instance
        controlling its lifecyle.
        """
        self.name = name
        self.container = container

    def prepare(self):
        """ Called when the service container starts.

        DependencyProviders should do any required initialisation here.
        """

    def start(self):
        """ Called when the service container has successfully started.

        This is only called after all other DependencyProviders have
        successfully initialised. If the DependencyProvider listens to
        external events, they may now start acting upon them.
        """

    def stop(self):
        """ Called when the service container begins to shut down.

        DependencyProviders should do any graceful shutdown here.
        """

    def kill(self, exc=None):
        """ Called to stop this dependency without grace. The exception
        causing the kill may be provided.

        DependencyProviders should urgently shut down here. This method must
        return within ``nameko.service.KILL_TIMEOUT`` seconds, otherwise it
        may be forcibly stopped.
        """

    def worker_setup(self, worker_ctx):
        """ Called before a service worker executes a task. This method is
        called for all DependencyProviders, not just the one that triggered
        the worker spawn.

        DependencyProviders should do any pre-processing here, raising
        exceptions in the event of failure.

        Example: ...

        Args:
            - worker_ctx: see ``nameko.service.ServiceContainer.spawn_worker``
        """

    def worker_teardown(self, worker_ctx):
        """ Called after a service worker has executed a task. This method is
        called for all DependencyProviders, not just the one that triggered
        the worker spawn.

        DependencyProviders should do any post-processing here, raising
        exceptions in the event of failure.

        Example: a database session provider may commit the session

        Args:
            - worker_ctx: see ``nameko.service.ServiceContainer.spawn_worker``
        """


class EntrypointProvider(DependencyProvider):
    pass


class InjectionProvider(DependencyProvider):
    __metaclass__ = ABCMeta

    @abstractmethod
    def acquire_injection(self, worker_ctx):
        """ A subclass must return the instance that should be injected
        into the worker instance of the service by the container.
        """

    def worker_result(self, worker_ctx, result=None, exc=None):
        """ Called with the result of a service worker execution.

        InjectionProvider that need to process the result should do it here.
        This method is called for all InjectionProviders on completion of any
        worker.

        Example: a database session provider may flush the transaction

        Args:
            - worker_ctx: see ``nameko.service.ServiceContainer.spawn_worker``
        """

    def inject(self, worker_ctx):
        injection = self.acquire_injection(worker_ctx)

        injection_name = self.name
        service = worker_ctx.service
        setattr(service, injection_name, injection)

    def release(self, worker_ctx):

        service = worker_ctx.service
        injection_name = self.name
        delattr(service, injection_name)


class DependencySet(SpawningSet):

    @property
    def injections(self):
        """ A ``SpawningSet`` of just the ``InjectionProvider`` instances in
        this set.
        """
        return SpawningSet([item for item in self
                           if is_injection_provider(item)])

    @property
    def entrypoints(self):
        """ A ``SpawningSet`` of just the ``EntrypointProvider`` instances in
        this set.
        """
        return SpawningSet([item for item in self
                            if is_entrypoint_provider(item)])


registered_dependencies = WeakSet()
registered_injections = WeakSet()


def register_dependency(factory):
    registered_dependencies.add(factory)


def register_injection(factory):
    registered_injections.add(factory)


def register_entrypoint(fn, provider):
    descriptors = getattr(fn, ENTRYPOINT_PROVIDERS_ATTR, None)

    if descriptors is None:
        descriptors = set()
        setattr(fn, ENTRYPOINT_PROVIDERS_ATTR, descriptors)

    descriptors.add(provider)


shared_dependencies = WeakKeyDictionary()


class DependencyFactory(object):

    shared = False

    def __init__(self, dep_cls, *init_args, **init_kwargs):
        self.dep_cls = dep_cls
        self.args = init_args
        self.kwargs = init_kwargs

    # TODO: doesn't stop "equal" copies being addded to a set
    # //--> use __hash__
    def __eq__(self, other):
        if not isinstance(other, DependencyFactory):
            return False
        return all([
            self.dep_cls is other.dep_cls,
            self.args == other.args,
            self.kwargs == other.kwargs
        ])

    def create_and_bind_instance(self, name, container):
        """ Instaniate an instance of ``dep_cls`` and bind it to ``container``.

        See `:meth:~DependencyProvider.bind`.
        """
        if self.shared:
            instance = shared_dependencies.get(self)
            if instance is None:
                instance = self.dep_cls(*self.args, **self.kwargs)
        else:
            instance = self.dep_cls(*self.args, **self.kwargs)
        instance.bind(name, container)
        return instance


def entrypoint(decorator_func):
    """ Transform a callable into a decorator that can be used to declare
    entrypoints.

    The callable must return a DependencyFactory that creates the
    EntrypointProvider instance.

    e.g::

        @entrypoint
        def http(bind_port=80):
            return DependencyFactory(HttpEntrypoint, bind_port)

        class Service(object):

            @http
            def foobar():
                pass

    """
    def registering_decorator(fn, args, kwargs):
        factory = decorator_func(*args, **kwargs)
        if not isinstance(factory, DependencyFactory):
            raise DependencyTypeError('Arguments to `entrypoint` must return '
                                      'DependencyFactory instances')
        register_entrypoint(fn, factory)
        return fn

    @wraps(decorator_func)
    def wrapper(*args, **kwargs):
        if len(args) == 1 and isinstance(args[0], types.FunctionType):
            # usage without arguments to the decorator:
            # @foobar
            # def spam():
            #     pass
            return registering_decorator(args[0], tuple(), {})
        else:
            # usage with arguments to the decorator:
            # @foobar('shrub', ...)
            # def spam():
            #     pass
            return partial(registering_decorator, args=args, kwargs=kwargs)

    return wrapper


def injection(fn):
    """ Transform a callable into a function that can be used to create
    injections.

    The callable must return a DependencyFactory that creates the
    InjectionProvider instance.

    e.g::

        @injection
        def database(*args, **kwargs):
            return DependencyFactory(DatabaseProvider, *args, **kwargs)

        class Service(object):

            db = database()
    """
    @wraps(fn)
    def wrapped(*args, **kwargs):
        factory = fn(*args, **kwargs)
        if not isinstance(factory, DependencyFactory):
            raise DependencyTypeError('Arguments to `injection` must return '
                                      'DependencyFactory instances')
        register_injection(factory)
        return factory
    return wrapped


def dependency(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        shared = kwargs.pop('shared', False)
        factory = fn(*args, **kwargs)

        if not isinstance(factory, DependencyFactory):
            raise DependencyTypeError('Arguments to `dependency` must return '
                                      'DependencyFactory instances')
        factory.shared = shared
        register_dependency(factory)
        return factory
    return wrapped


def is_injection_provider(obj):
    return isinstance(obj, InjectionProvider)


def is_entrypoint_provider(obj):
    return isinstance(obj, EntrypointProvider)


def get_dependency_providers(container, provider):

    def get_dependencies(obj):
        import collections
        for name, attr in inspect.getmembers(obj, lambda x: isinstance(x, collections.Hashable)):
            if attr in registered_dependencies:
                factory = attr
                provider = factory.create_and_bind_instance(name, container)
                for prov in get_dependencies(provider):
                    yield prov
                yield provider

    for dep in get_dependencies(provider):
        yield dep


def get_injection_providers(container, include_dependencies=False):
    service_cls = container.service_cls
    for name, attr in inspect.getmembers(service_cls):
        if attr in registered_injections:
            factory = attr
            provider = factory.create_and_bind_instance(name, container)
            yield provider
            if include_dependencies:
                for dependency in get_dependency_providers(container, provider):
                    yield dependency


def get_entrypoint_providers(container, include_dependencies=False):
    service_cls = container.service_cls
    for name, attr in inspect.getmembers(service_cls, inspect.ismethod):
        factories = getattr(attr, ENTRYPOINT_PROVIDERS_ATTR, [])
        for factory in factories:
            provider = factory.create_and_bind_instance(name, container)
            yield provider
            if include_dependencies:
                for dependency in get_dependency_providers(container, provider):
                    yield dependency


def get_dependencies(container):
    return chain(get_injection_providers(container, include_dependencies=True),
                 get_entrypoint_providers(container, include_dependencies=True))
