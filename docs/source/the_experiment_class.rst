The Experiment Class
====================

Experiments are designed in Dallinger by creating a custom subclass of the base
Experiment class. The code for the Experiment class is in experiments.py.
Unlike the :doc:`other classes <classes>`, each experiment involves only a
single Experiment object and it is not stored as an entry in a corresponding
table, rather each Experiment is a set of instructions that tell the server
what to do with the database when the server receives requests from outside.

.. module:: dallinger.experiment

.. autoclass:: Experiment

  .. autoattribute:: verbose
    :annotation:

  .. autoattribute:: task
    :annotation:

  .. autoattribute:: session
    :annotation:

  .. autoattribute:: practice_repeats
    :annotation:

  .. autoattribute:: experiment_repeats
    :annotation:

  .. autoattribute:: recruiter
    :annotation:

  .. autoattribute:: qualifications
    :annotation:

  .. autoattribute:: group_qualifications
    :annotation:

  .. autoattribute:: qualification_active
    :annotation:

  .. autoattribute:: initial_recruitment_size
    :annotation:

  .. autoattribute:: known_classes
    :annotation:

  .. autoattribute:: participant_constructor
    :annotation:

  .. attribute:: public_properties

     dictionary, the properties of this experiment that are exposed
     to the public over an AJAX call

  .. automethod:: __init__

  .. automethod:: add_node_to_network

  .. automethod:: assignment_abandoned

  .. automethod:: assignment_reassigned

  .. automethod:: assignment_returned

  .. automethod:: attention_check

  .. automethod:: attention_check_failed

  .. automethod:: bonus

  .. automethod:: bonus_reason

  .. automethod:: collect

  .. automethod:: create_network

  .. automethod:: create_node

  .. automethod:: create_participant

  .. automethod:: dashboard_database_actions

  .. automethod:: dashboard_fail

  .. automethod:: data_check

  .. automethod:: data_check_failed

  .. automethod:: events_for_replay

  .. automethod:: extra_parameters

  .. automethod:: exit_info_for

  .. automethod:: fail_participant

  .. automethod:: get_network_for_participant

  .. automethod:: info_get_request

  .. automethod:: info_post_request

  .. automethod:: is_complete

  .. automethod:: is_overrecruited

  .. automethod:: load_participant

  .. automethod:: log

  .. automethod:: log_summary

  .. automethod:: make_uuid

  .. automethod:: networks

  .. automethod:: node_get_request

  .. automethod:: node_post_request

  .. automethod:: normalize_entry_information

  .. automethod:: participant_task_completed

  .. automethod:: recruit

  .. automethod:: replay_event

  .. automethod:: replay_start

  .. automethod:: replay_finish

  .. automethod:: replay_started

  .. automethod:: run

  .. automethod:: save

  .. automethod:: setup

  .. automethod:: submission_successful

  .. automethod:: transformation_get_request

  .. automethod:: transformation_post_request

  .. automethod:: transmission_get_request

  .. automethod:: transmission_post_request

  .. automethod:: vector_get_request

  .. automethod:: vector_post_request
