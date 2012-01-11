'''
Manage a local persistent data structure that can hold any arbitrairy data
specific to the minion
'''

import os
import salt.payload
import ast

def load():
    '''
    Return all of the data in the minion datastore

    CLI Example::

        salt '*' data.load
    '''
    serial = salt.payload.Serial(__opts__)

    try:
        fn_ = open(os.path.join(__opts__['cachedir'], 'datastore'), "r")
        return serial.load(fn_)
    except:
        return {}

def dump(new_data):
    '''
    Replace the entire datastore with a passed data structure

    CLI Example::

        salt '*' data.dump '{'eggs': 'spam'}' 
    '''
    if not isinstance(new_data, dict):
        if isinstance(ast.literal_eval(new_data), dict):
            new_data = ast.literal_eval(new_data)
        else:
            return False
    
    try:
        fn_ = open(os.path.join(__opts__['cachedir'], 'datastore'), "w")
        
        serial = salt.payload.Serial(__opts__)
        serial.dump(new_data, fn_)

        return True

    except:
        return False

def update(key, value):
    '''
    Update a key with a value in the minion datastore

    CLI Example::

        salt '*' data.update <key> <value>
    '''
    store = load()
    store[key] = value
    dump(store)
    return True

def getval(key):
    '''
    Get a value from the minion datastore

    CLI Example::
        
        salt '*' data.get_value <key>
    
    '''
    store = load()
    return store[key]

def getvals(keys):
    '''
    Get a value from the minion datastore

    CLI Example::
        
        salt '*' data.get_value <key>
    
    '''
    store = load()
    ret = []
    for key in keys:
        ret[key] = store[key]
    return ret
